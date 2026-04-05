from google.adk.agents import Agent, SequentialAgent, LoopAgent
from google.adk.tools import exit_loop
from tools.render_tool import render_manim_video

# 1. THE RESEARCHER: Breaks broad topics into focused JSON curriculums.
#    Runs standalone in Phase 1, before parallel subtopic processing.
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

# 3. THE GENERATOR: Writes the initial Manim Python code from the directive.
manim_generator = Agent(
    name="manim_generator",
    model="gemini-2.5-flash",
    description="Generates initial Manim Python code from a script.",
    instruction="""You are an expert Manim Community Edition (v0.18) developer.
    Your strictly focused job is to translate a storyboard script into a precise Manim animation.

    Script: {SCRIPT_JSON?}

    Follow these STRICT RULES:
    1.  **API Version:** ONLY use ManimCE v0.18 syntax.
    2.  **Creation:** Use `Create()` for drawing lines/shapes, `Write()` for text/math, and `FadeIn()` for images/groups. NEVER use `ShowCreation()`.
    3.  **Color:** Pass `color=...` in the constructor (e.g., `Text("Hello", color=BLUE)`), do not use `.set_color()`.
    4.  **Positioning:** Elements MUST NOT overlap. Use `mobj.next_to()`, `mobj.shift()`, `mobj.to_edge()`, or `mobj.move_to()` explicitly.
    5.  **Graphs:** Use `axes.plot(func)` NOT `axes.get_graph(func)`.
    6.  **Grouping:** Use `VGroup` for Mobjects of the same type.
    7.  **Text vs Math:** Use `MathTex` strictly for equations and `Text` for plain text.
    8.  **Pacing:** End every major animation block with `self.wait(1)` and the end of the scene with `self.wait(2)`.
    9.  **No images or SVGs:** Do NOT use ImageMobject or SVGMobject.

    Output ONLY raw Python code. No markdown blocks, no backticks, no explanation.
    The class MUST be named `GeneratedScene`.
    """,
    output_key="MANIM_CODE",
)

# 4. THE FIXER: Triggers on render failure to debug and verify the code.
manim_fixer = Agent(
    name="manim_fixer",
    model="gemini-2.5-flash",
    description="Debugs, holistically reviews, and fixes failed Manim Python code.",
    instruction="""You are an expert Manim Community Edition (v0.18) debugging specialist and code reviewer.
    The previous render failed. Your job is to fix the code and ensure it is flawless.

    Original Script Intent: {SCRIPT_JSON?}
    Failed Code: {MANIM_CODE?}
    Python Traceback / Error: {RENDER_ERROR?}

    DIAGNOSTIC & VERIFICATION RULES:
    1.  **Traceback Analysis:** Identify the exact cause of the provided traceback.
    2.  **Holistic Review (No Whack-a-Mole):** DO NOT just fix the single line that failed. You MUST scan the ENTIRE provided code for similar anti-patterns, deprecated methods, or identical mistakes and fix ALL of them globally.
    3.  **Strict Compliance Check:** Verify the whole script against these common ManimCE pitfalls:
        - Are `VGroup`s only containing the exact same Mobject types? (If mixed, change to `Group`).
        - Are there ANY instances of `ShowCreation`? (Change all to `Create`).
        - Are colors passed in constructors (e.g., `Text("...", color=BLUE)`) rather than `.set_color()`?
        - Do any objects overlap? (Ensure `next_to`, `shift`, or `to_edge` are used).
        - No ImageMobject or SVGMobject.
    4.  **Mental Dry-Run:** Before outputting, mentally step through the animation. Ensure standard pacing (`self.wait()`) is present and no objects are drawn off-screen.

    Output ONLY the FULL, corrected raw Python code. No markdown blocks, no backticks, no explanation.
    The class MUST remain named `GeneratedScene`.
    """,
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
# Used by main.py to run N instances in parallel via asyncio.gather.
subtopic_pipeline = SequentialAgent(
    name="subtopic_pipeline",
    description="Processes a single subtopic: script → code → render+fix.",
    sub_agents=[scriptwriter, manim_generator, render_and_fix_loop],
)
