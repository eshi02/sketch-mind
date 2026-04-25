import asyncio
import logging
import os
import pathlib
from typing import AsyncGenerator

from typing_extensions import override

from google.adk.agents import Agent, BaseAgent, SequentialAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.tools.mcp_tool import McpToolset
from mcp import StdioServerParameters
from tools.render_tool import render_manim_video

logger = logging.getLogger("sketchmind-agents")

# Allow flipping models without redeploying. Defaults are tuned for
# Vertex AI's global endpoint (preview models live there).
PRO_MODEL = os.getenv("AGENT_PRO_MODEL", "gemini-3.1-pro-preview")
FLASH_MODEL = os.getenv("AGENT_FLASH_MODEL", "gemini-3-flash-preview")


class RenderAgent(BaseAgent):
    """Deterministic render step.

    Reads MANIM_CODE and AUDIO_SCRIPT from state, calls render_manim_video,
    writes VIDEO_URL on success (and escalates the LoopAgent) or RENDER_ERROR
    on failure (and lets the LoopAgent move on to the fixer).
    """

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        manim_code = state.get("MANIM_CODE", "")
        audio_script = state.get("AUDIO_SCRIPT", "")

        if not audio_script:
            logger.warning("[RenderAgent] AUDIO_SCRIPT is empty — video will be silent.")

        if not manim_code:
            state["RENDER_ERROR"] = "MANIM_CODE missing from session state"
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                actions=EventActions(
                    state_delta={"RENDER_ERROR": state["RENDER_ERROR"]}
                ),
            )
            return

        # render_manim_video is sync (uses httpx.post). Off-thread it so the
        # event loop stays responsive — a single render is 30-240s.
        result = await asyncio.to_thread(render_manim_video, manim_code, audio_script)

        if result.get("status") == "success" and result.get("video_url"):
            video_url = result["video_url"]
            state["VIDEO_URL"] = video_url
            state["RENDER_ERROR"] = ""
            logger.info(f"[RenderAgent] success → {video_url}")
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                actions=EventActions(
                    state_delta={"VIDEO_URL": video_url, "RENDER_ERROR": ""},
                    escalate=True,
                ),
            )
            return

        # Failure path — surface the error for the fixer.
        err = result.get("error", "Unknown render error")
        # Truncate so the fixer's prompt doesn't blow up token budget.
        err_short = err[-1500:] if len(err) > 1500 else err
        state["RENDER_ERROR"] = err_short
        logger.info(f"[RenderAgent] failure → {err_short[:200]}")
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={"RENDER_ERROR": err_short}),
        )

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
        model=FLASH_MODEL,
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

    # 2. THE SCRIPTWRITER: Visual-only storyboard (narration is handled by the
    #    narrator agent after the Manim code is generated).
    scriptwriter = Agent(
        name="scriptwriter",
        model=FLASH_MODEL,
        description="Creates a precise, continuous Manim visual storyboard from a subtopic brief.",
        instruction="""You are a technical video director for animated educational videos.
    Translate the provided research brief into a detailed visual storyboard.

    Research Brief: {SUBTOPIC_DATA?}

    Output ONLY a single JSON object representing a 90-120 second video.

    CRITICAL DESIGN PRINCIPLES:
    1. **ANIMATION-FIRST:** The video must be driven by moving shapes, graphs, arrows, transforms,
       and geometric objects — NOT walls of text. Text should only be short labels (1-5 words),
       titles, or key terms. NEVER display full sentences as on-screen text.
    2. **SHOW, DON'T TELL:** If explaining gravity, animate a ball falling — don't write "gravity
       pulls objects down" on screen.
    3. **MINIMAL ON-SCREEN TEXT:** Maximum 5 words per text element. Use at most 3 text elements
       visible at any time. Prefer MathTex for formulas and short Text labels for key terms.
    4. **SAFE LAYOUT:** All elements must stay within x=[-6, 6] and y=[-3.5, 3.5].
    5. **CLEAN TRANSITIONS:** FadeOut ALL previous elements before introducing new ones.
    6. **KEEP VISUALS SIMPLE:** Use only basic shapes: circles, rectangles, arrows, lines, dots, axes.
       Represent complex concepts using SIMPLE metaphors with basic geometric shapes.
    7. **MAX 5 ELEMENTS:** Never have more than 5 visible elements on screen simultaneously.

    VISUAL DIRECTIVE RULES:
    1. One continuous flow — no separate scenes.
    2. Target 6-10 chronological steps for a rich, detailed animation.
    3. Use specific verbs: "Create", "Write", "Transform", "FadeIn", "FadeOut", "MoveAlongPath", "Indicate".
    4. Specify exact positions: "at UP*2+LEFT*3", "at center", "at DOWN*1.5".
    5. Specify object types: MathTex, Text (short labels only), Axes, Circle, Arrow, Line, Rectangle.
    6. Emphasize animated transitions: objects morphing, growing, moving, being highlighted.
    7. Each step MUST include FadeOut of previous elements if the area will be reused.
    8. Include "Wait 2 sec" after major animation moments to give viewers time to absorb.
    9. End with "Wait 3 sec" for a clean outro.

    JSON SCHEMA:
    {
      "concept_title": "The title of the concept",
      "continuous_visual_directive": "Step 1: Write short Text 'Title' at UP*3. Wait 2 sec. Step 2: Create Circle at center. Wait 2 sec. Step 3: FadeOut Text. Create Axes at center. Wait 2 sec. ..."
    }

    No markdown blocks around the JSON, no conversational text. ONLY raw JSON.
    """,
        output_key="SCRIPT_JSON",
    )

    # 3. THE GENERATOR: Writes initial Manim code — uses MCP tools for API reference.
    manim_generator = Agent(
        name="manim_generator",
        model=PRO_MODEL,
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
    13. **Pacing:** `self.wait(2)` after every major animation block. `self.wait(3)` at scene end.
        Target 5-8 distinct visual sections for a rich 90-120 second video.
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
        model=PRO_MODEL,
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

    # 5. THE NARRATOR: Writes voiceover from the actual Manim code so narration
    #    matches the visual sequence exactly.
    narrator = Agent(
        name="narrator",
        model=FLASH_MODEL,
        description="Writes narration synchronized to the actual Manim animation code.",
        instruction="""You are a video narrator for educational animations.
    You are given the actual Python animation code and the original concept brief.
    Your job is to write narration that EXACTLY matches what appears on screen.

    Animation Code:
    {MANIM_CODE?}

    Original Concept:
    {SCRIPT_JSON?}

    TIMING ANALYSIS — read the code and estimate timing:
    - Each `self.play(...)` call takes approximately 1 second
    - Each `self.wait(X)` call takes X seconds
    - `self.play(FadeOut(*self.mobjects))` is a scene transition, approximately 1 second

    RULES:
    1. Walk through the code TOP TO BOTTOM. For each visual section
       (between scene clears), write narration that explains what the viewer is seeing.
    2. Match your word count to each section's duration.
       At normal speaking pace, aim for approximately 2.5 words per second.
       For example, a 10-second section needs about 25 words of narration.
    3. Write plain conversational English. No special characters, no markdown,
       no asterisks, no parentheses, no formatting of any kind.
    4. The narration should TEACH the concept, not describe the code.
       Say "Notice how the triangle forms" not "A triangle object is being created."
    5. Use natural pauses by ending sentences where the code has self.wait() calls.
    6. Keep the tone warm and educational, like a teacher explaining to a student.

    Output ONLY the narration text as a single continuous paragraph.
    No JSON, no markdown, no backticks, no explanation.
    """,
        output_key="AUDIO_SCRIPT",
    )

    # Deterministic renderer step.
    renderer_agent = RenderAgent(
        name="renderer",
        description="Renders Manim code into video and writes VIDEO_URL on success.",
    )

    # Loop: render → if error, fixer rewrites MANIM_CODE → re-render.
    render_and_fix_loop = LoopAgent(
        name="render_and_fix_loop",
        description="Renders Manim code and retries with fixer agent on errors.",
        sub_agents=[renderer_agent, manim_fixer],
        max_iterations=3,
    )

    # Subtopic pipeline: script → code → narration → render+fix.
    subtopic_pipeline = SequentialAgent(
        name="subtopic_pipeline",
        description="Processes a single subtopic: script → code → narrate → render+fix.",
        sub_agents=[scriptwriter, manim_generator, narrator, render_and_fix_loop],
    )

    return researcher, subtopic_pipeline, mcp_toolset
