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
        instruction="""You are a technical video scriptwriter and director for animated educational videos.
    Translate the provided research brief into a visual-first storyboard.

    Research Brief: {SUBTOPIC_DATA?}

    Output ONLY a single JSON object representing the entire 60-90 second video.

    CRITICAL DESIGN PRINCIPLES:
    1. **ANIMATION-FIRST:** The video must be driven by moving shapes, graphs, arrows, transforms,
       and geometric objects — NOT walls of text. Text should only be short labels (1-5 words),
       titles, or key terms. NEVER display full sentences as on-screen text.
    2. **SHOW, DON'T TELL:** If explaining gravity, animate a ball falling — don't write "gravity
       pulls objects down" on screen. The audio script carries the explanation; the visuals illustrate it.
    3. **MINIMAL ON-SCREEN TEXT:** Maximum 5 words per text element. Use at most 3 text elements
       visible at any time. Prefer MathTex for formulas and short Text labels for key terms.
    4. **SAFE LAYOUT:** All elements must stay within the visible frame. Use coordinates within
       x=[-6, 6] and y=[-3.5, 3.5]. Never place elements at extreme edges.
    5. **CLEAN TRANSITIONS:** FadeOut ALL previous elements before introducing new ones.
       Never let elements pile up or overlap. Each visual section starts on a clean canvas.
    6. **KEEP VISUALS SIMPLE:** Use only basic shapes: circles, rectangles, arrows, lines, dots, axes.
       Do NOT describe complex structures like molecular diagrams, circuit boards, DNA helices, or
       detailed anatomical drawings. Instead, represent complex concepts using SIMPLE metaphors
       with basic geometric shapes. For example: represent atoms as colored circles, bonds as lines,
       data flow as arrows between boxes.
    7. **MAX 5 ELEMENTS:** Never have more than 5 visible elements on screen simultaneously.

    VISUAL DIRECTIVE RULES:
    1. One continuous flow — no separate scenes.
    2. Chronological steps: "Step 1: ..., Step 2: ..., Step 3: ...".
    3. Use specific verbs: "Create", "Write", "Transform", "FadeIn", "FadeOut", "MoveAlongPath", "Indicate".
    4. Specify exact positions: "at UP*2+LEFT*3", "at center", "at DOWN*1.5".
    5. Specify object types: MathTex, Text (short labels only), Axes, Circle, Arrow, Line, Rectangle.
    6. Emphasize animated transitions: objects morphing, growing, moving, being highlighted.
    7. Each step MUST include FadeOut of previous elements if the area will be reused.

    AUDIO SCRIPT RULES:
    1. Write the full_audio_script as natural spoken narration — as if a teacher is explaining.
    2. Do NOT include any special characters, markdown, asterisks, bullet points, or formatting.
    3. Write plain conversational English only. No parentheses, no slashes, no technical markup.
    4. Example GOOD: "Lets start with the Pythagorean theorem. It tells us that in a right triangle, the square of the longest side equals the sum of the squares of the other two sides."
    5. Example BAD: "The *Pythagorean* theorem states: a² + b² = c² (where c = hypotenuse)."

    JSON SCHEMA:
    {
      "concept_title": "The title of the concept",
      "full_audio_script": "Natural spoken narration without any special characters or formatting...",
      "continuous_visual_directive": "Step 1: Write short Text 'Title' at UP*3. Wait 1 sec. Step 2: Create Circle at center. Step 3: FadeOut Text. Create Axes at center...",
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
    4.  **SAFE FRAME BOUNDS:** The visible frame is x=[-7, 7], y=[-4, 4]. Keep ALL elements within
        x=[-6, 6] and y=[-3.5, 3.5] to prevent clipping. Use `.scale()` to shrink elements if needed.
    5.  **MANDATORY SCENE CLEARING:** Before EVERY new visual section, call
        `self.play(FadeOut(*self.mobjects))` to clear the entire scene. This is NON-NEGOTIABLE.
        Never accumulate elements from previous sections. Each section starts with a clean canvas.
    6.  **NO OVERLAPPING — EVER:** Never place two elements at the same position. Mentally track
        every element's position. Use `next_to()`, `shift()`, or explicit coordinates with sufficient spacing.
        If in doubt, clear the scene first with `self.play(FadeOut(*self.mobjects))`.
    7.  **KEEP IT SIMPLE:** Use only basic Manim primitives: Circle, Square, Rectangle, Arrow, Line,
        Dot, Text, MathTex, Axes, NumberLine, VGroup. Do NOT attempt complex structures like
        molecular diagrams, circuit boards, neural networks, or anything requiring precise multi-element layouts
        with many overlapping parts. Represent complex concepts with SIMPLE analogies using basic shapes.
    8.  **SHORT TEXT ONLY:** Text objects must be 1-5 words max. NEVER put full sentences on screen.
        Use `font_size=36` or smaller for labels. Use `font_size=48` only for titles.
    9.  **MAX 5 ELEMENTS ON SCREEN:** At any point, no more than 5 visible elements on screen.
        If you need more, FadeOut older ones first.
    10. **ANIMATION-HEAVY:** Prefer animated objects (shapes, arrows, graphs, transforms) over text.
        At least 70% of the scene time should be geometric animations, not text appearing.
    11. **Graphs:** Use `axes.plot(func)` — verify with `lookup_manim_class("Axes")`.
    12. **Grouping:** Use `VGroup` for VMobjects of the same type, `Group` for mixed types.
    13. **Pacing:** `self.wait(1)` after major animation blocks, `self.wait(2)` at scene end.
    14. **No images or SVGs:** Do NOT use ImageMobject or SVGMobject.

    ANTI-PATTERN EXAMPLES (NEVER do these):
    - Placing 10+ Text/MathTex labels around a shape (causes overlap mess)
    - Building molecule/atom diagrams with individual letter labels (too complex for Manim)
    - Adding elements without clearing previous ones (causes pile-up)
    - Using coordinates without checking if another element is already there

    CORRECT PATTERN:
    ```
    # Section 1
    title = Text("Topic", font_size=48).to_edge(UP)
    self.play(Write(title))
    shape = Circle(radius=1.5, color=BLUE).move_to(ORIGIN)
    self.play(Create(shape))
    self.wait(1)
    # Clear before section 2
    self.play(FadeOut(*self.mobjects))
    # Section 2 — clean canvas
    ...
    ```

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
    The previous render failed. Your job is to fix the code, verify EVERY class and method, and ensure it is flawless.

    Original Script Intent: {SCRIPT_JSON?}
    Failed Code: {MANIM_CODE?}
    Python Traceback / Error: {RENDER_ERROR?}

    === PHASE 1: FIX THE ERROR ===
    1. Analyze the traceback to identify the root cause.
    2. Use `lookup_manim_class` to verify the CORRECT constructor signature of the class that caused the error.
    3. Use `search_manim_api` if the error suggests a deprecated or non-existent method.
    4. Fix the root cause.

    === PHASE 2: FULL CODE AUDIT (MANDATORY) ===
    After fixing the error, you MUST audit the ENTIRE code top-to-bottom. Do NOT skip this phase.

    For EVERY Manim class used in the code (Text, MathTex, Axes, Circle, Arrow, VGroup, etc.):
    1. Call `lookup_manim_class` to verify its constructor signature.
    2. Check that all arguments passed match the verified signature.
    3. If any argument is wrong, fix it immediately.

    Checklist — verify ALL of these before outputting:
    [ ] Every Manim class constructor matches the verified API signature
    [ ] No deprecated methods: ShowCreation→Create, get_graph→plot, ShowPassingFlash→correct alternative
    [ ] VGroup contains only VMobjects (use Group for mixed types)
    [ ] No ImageMobject or SVGMobject
    [ ] All elements within x=[-6, 6] and y=[-3.5, 3.5] — use .scale() if too large
    [ ] No overlapping elements — track positions mentally, use next_to/shift/to_edge
    [ ] Scene cleared with self.play(FadeOut(*self.mobjects)) between visual sections
    [ ] Max 5 elements visible on screen at any time
    [ ] Text elements are 1-5 words max, font_size=36 or smaller (48 for titles only)
    [ ] self.wait(1) after major animation blocks
    [ ] All imports are correct and present

    === PHASE 3: SIMPLIFY IF NEEDED ===
    If the code has more than 3 issues or tries to build complex structures (molecule diagrams,
    circuit boards, neural networks, detailed layouts with 10+ elements), REWRITE it from scratch
    using only simple shapes: Circle, Square, Rectangle, Arrow, Line, Dot, Text, MathTex, Axes.
    A simple animation that renders is ALWAYS better than a complex one that fails.

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
        instruction="""Call render_manim_video with the Manim code and the audio narration script.

    Code: {MANIM_CODE?}
    Script JSON: {SCRIPT_JSON?}

    IMPORTANT RULES:
    1. Parse the Script JSON above to extract the "full_audio_script" field.
    2. Call render_manim_video exactly ONCE with both python_code and audio_script parameters.
    3. If the result status is 'success', call exit_loop tool immediately.
    4. If the result status is 'error', just output the error message as plain text.
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
