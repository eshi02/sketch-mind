import pathlib
from google.adk.agents import Agent, SequentialAgent, LoopAgent
from google.adk.tools import exit_loop
from google.adk.tools.mcp_tool import McpToolset
from mcp import StdioServerParameters
from tools.render_tool import render_manim_video

# Path to the MCP server script
_MCP_SERVER_PATH = str(pathlib.Path(__file__).parent / "mcp_servers" / "manim_api_server.py")


async def create_agents():
    """Async factory that initializes MCP toolsets and builds the full agent graph.

    Returns (researcher, subtopic_pipeline, mcp_toolset).
    The mcp_toolset must be kept alive for the app lifetime and closed on shutdown.
    """
    mcp_toolset = McpToolset(
        connection_params=StdioServerParameters(
            command="python",
            args=[_MCP_SERVER_PATH],
        )
    )
    manim_tools = await mcp_toolset.get_tools()

    # 1. THE RESEARCHER: Breaks broad topics into focused JSON curriculums.
    researcher = Agent(
        name="researcher",
        model="gemini-2.5-flash",
        description="Researches a broad topic and breaks it down into structured subtopics.",
        instruction="""You are a technical curriculum architect. Your job is to take a user's topic and break it down into a logical progression of highly focused subtopics suitable for 60-90 second educational videos.

    RULES:
    1. If the topic is simple, output an array with 1 subtopic.
    2. If the topic is complex, break it into 2-4 sequential subtopics.
    3. Output ONLY a valid JSON array matching this schema:
    [
      {
        "subtopic_title": "...",
        "core_concept": "A 2-sentence explanation of the specific concept.",
        "key_formulas": ["formula 1", "formula 2"],
        "visual_metaphor": "A simple idea for how to visualize this using basic geometry or graphs."
      }
    ]
    No markdown blocks around the JSON, no conversational text. ONLY raw JSON.
    """,
        output_key="CURRICULUM_JSON",
    )

    # 2. THE SCRIPTWRITER: Translates a single subtopic into a continuous timeline.
    scriptwriter = Agent(
        name="scriptwriter",
        model="gemini-2.5-flash",
        description="Creates a precise, continuous Manim storyboard script from a subtopic brief.",
        instruction="""You are a technical video scriptwriter and director.
    Translate the provided single-concept research brief into a strict, continuous storyboard.

    Research Brief: {SUBTOPIC_DATA?}

    Output ONLY a single JSON object representing the entire 60-90 second video.
    RULES:
    1. Do NOT break the video into separate scenes. It must be one continuous flow.
    2. The `continuous_visual_directive` MUST dictate exact geometry and timing chronologically (e.g., "Step 1: ..., Step 2: ..., Step 3: ...").
    3. Use specific verbs: "Create", "Write", "Transform", "FadeIn".
    4. Specify locations: "top edge", "center", "next to".
    5. Specify object types: Use "MathTex" for equations, "Text" for words, "Axes" for graphs.

    JSON SCHEMA:
    {
      "concept_title": "The title of the concept",
      "full_audio_script": "The complete spoken words for the voiceover...",
      "continuous_visual_directive": "Step 1: Write Text 'Title' at the top edge. Wait 1 sec. Step 2: Create a set of Axes in the center...",
      "estimated_duration_seconds": 85
    }

    No markdown blocks around the JSON, no conversational text. ONLY raw JSON.
    """,
        output_key="SCRIPT_JSON",
    )

    # 3. THE GENERATOR: Writes initial Manim code — uses MCP tools for API reference.
    manim_generator = Agent(
        name="manim_generator",
        model="gemini-2.5-flash",
        description="Generates initial Manim Python code from a script.",
        instruction="""You are an expert Manim Community Edition (v0.20.1) developer.
    Your job is to translate a storyboard script into a precise Manim animation.

    Script: {SCRIPT_JSON?}

    WORKFLOW — follow these steps in order:
    1. Use `list_manim_animations` to see all available animation classes.
    2. Use `lookup_manim_class` to verify the constructor signature of EVERY Manim class
       you plan to use (Text, MathTex, Axes, Circle, Arrow, VGroup, etc.).
    3. If unsure how to achieve a visual effect, use `search_manim_api` to find the right class.
    4. Only AFTER verifying signatures, write the complete Python code.

    STRICT RULES:
    1.  **API Version:** ONLY use ManimCE v0.20.1 syntax. Verify with lookup tools.
    2.  **Creation:** Use `Create()` for shapes, `Write()` for text/math, `FadeIn()` for groups. NEVER use `ShowCreation()`.
    3.  **Color:** Pass `color=...` in the constructor (e.g., `Text("Hello", color=BLUE)`).
    4.  **Positioning:** Elements MUST NOT overlap. Use `next_to()`, `shift()`, `to_edge()`, or `move_to()`.
    5.  **Graphs:** Use `axes.plot(func)` — verify with `lookup_manim_class("Axes")`.
    6.  **Grouping:** Use `VGroup` for VMobjects of the same type, `Group` for mixed types.
    7.  **Text vs Math:** Use `MathTex` for equations, `Text` for plain text.
    8.  **Pacing:** `self.wait(1)` after major animation blocks, `self.wait(2)` at scene end.
    9.  **No images or SVGs:** Do NOT use ImageMobject or SVGMobject.

    Output ONLY raw Python code. No markdown blocks, no backticks, no explanation.
    The class MUST be named `GeneratedScene`.
    """,
        tools=manim_tools,
        output_key="MANIM_CODE",
    )

    # 4. THE FIXER: Triggers on render failure — uses MCP tools to verify fixes.
    manim_fixer = Agent(
        name="manim_fixer",
        model="gemini-2.5-flash",
        description="Debugs, holistically reviews, and fixes failed Manim Python code.",
        instruction="""You are an expert Manim Community Edition (v0.20.1) debugging specialist.
    The previous render failed. Your job is to fix the code and ensure it is flawless.

    Original Script Intent: {SCRIPT_JSON?}
    Failed Code: {MANIM_CODE?}
    Python Traceback / Error: {RENDER_ERROR?}

    WORKFLOW:
    1. Analyze the traceback to identify the root cause.
    2. Use `lookup_manim_class` to verify the CORRECT signature of any class mentioned
       in the error (e.g., if TypeError about args, look up the actual constructor).
    3. Use `search_manim_api` if the error suggests a deprecated or non-existent method.
    4. Scan the ENTIRE code for similar anti-patterns and fix ALL of them.

    DIAGNOSTIC RULES:
    1.  **Holistic Review:** DO NOT just fix the single line that failed. Scan the ENTIRE code
        for similar anti-patterns, deprecated methods, or identical mistakes.
    2.  **Type Safety:** Are `VGroup`s only containing VMobjects? (If mixed, use `Group`).
    3.  **Deprecated Methods:** No `ShowCreation` (use `Create`). No `get_graph` (use `plot`).
    4.  **Positioning:** Ensure no objects overlap. Use `next_to`, `shift`, `to_edge`.
    5.  **No ImageMobject or SVGMobject.**
    6.  **Mental Dry-Run:** Step through the animation mentally. Ensure pacing and no off-screen objects.

    Output ONLY the FULL, corrected raw Python code. No markdown blocks, no backticks, no explanation.
    The class MUST remain named `GeneratedScene`.
    """,
        tools=manim_tools,
        output_key="MANIM_CODE",
    )

    # Renderer agent: calls render tool, exits loop on success
    renderer_agent = Agent(
        name="renderer",
        model="gemini-2.5-flash",
        description="Renders Manim code into video.",
        instruction="""Call render_manim_video ONCE with this code: {MANIM_CODE?}

    IMPORTANT RULES:
    1. Call render_manim_video exactly ONCE. Never call it more than once.
    2. If the result status is 'success', call exit_loop tool immediately.
    3. If the result status is 'error', just output the error message as plain text.
       Do NOT call render_manim_video again. Do NOT call exit_loop.
       Just output the error so the fixer can fix it in the next iteration.""",
        tools=[render_manim_video, exit_loop],
        output_key="RENDER_ERROR",
    )

    # Loop: render → if error, fixer rewrites MANIM_CODE → re-render (max 5 iterations)
    render_and_fix_loop = LoopAgent(
        name="render_and_fix_loop",
        description="Renders Manim code and retries with fixer agent on errors.",
        sub_agents=[renderer_agent, manim_fixer],
        max_iterations=5,
    )

    # Subtopic pipeline: processes a single subtopic end-to-end.
    subtopic_pipeline = SequentialAgent(
        name="subtopic_pipeline",
        description="Processes a single subtopic: script → code → render+fix.",
        sub_agents=[scriptwriter, manim_generator, render_and_fix_loop],
    )

    return researcher, subtopic_pipeline, mcp_toolset
