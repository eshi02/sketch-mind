from google.adk.agents import Agent, SequentialAgent, LoopAgent
from google.adk.tools import exit_loop
from tools.render_tool import render_manim_video

researcher = Agent(
    name="researcher",
    model="gemini-2.5-flash",
    description="Researches topics thoroughly.",
    instruction="""You are a research specialist. Given a topic, provide accurate info.
    Output a structured brief (max 400 words) with core concepts,
    definitions, formulas, and examples.
    Save your research to the state key RESEARCH.""",
    output_key="RESEARCH",
)

scriptwriter = Agent(
    name="scriptwriter",
    model="gemini-2.5-flash",
    description="Creates video scripts as JSON from research.",
    instruction="""You are a scriptwriter. Use the research below to create a video script.

    Research: {RESEARCH?}

    Output ONLY a JSON array:
    [{"scene_number":1,"audio_script":"...","visual_directive":"...","duration_seconds":10}]
    RULES: 4-6 scenes, 60-90 sec total. visual_directive: ONLY simple Manim-drawable
    objects (Axes with axes.plot(), Circle, Rectangle, Arrow, Text, MathTex).
    No images, no SVGs. Keep visuals simple and achievable.
    Start with title, end with summary. Output ONLY valid JSON.""",
    output_key="SCRIPT",
)

manim_coder = Agent(
    name="manim_coder",
    model="gemini-2.5-flash",
    description="Generates Manim Python code from a script.",
    instruction="""You generate Manim Community Edition v0.18 Python code.

    Script: {SCRIPT?}
    Previous render error (if any): {RENDER_ERROR?}

    Output ONLY raw Python code. No markdown, no backticks, no explanation.
    RULES:
    - from manim import *
    - Class name MUST be GeneratedScene
    - ManimCE v0.18 syntax only
    - No ImageMobject/SVGMobject
    - Use Create not ShowCreation
    - Use color= in constructor not .set_color()
    - Use axes.plot(func) NOT axes.get_graph(func)
    - Use Group instead of VGroup when mixing Mobject types
    - Use MathTex for math, Text for plain text
    - Keep animations simple and reliable: Text, MathTex, Circle, Rectangle, Arrow, Axes, axes.plot()
    - Avoid complex or obscure Manim features
    If there is a RENDER_ERROR, fix only that error and output the full corrected code.""",
    output_key="MANIM_CODE",
)

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
       Just output the error so the coder can fix it in the next iteration.""",
    tools=[render_manim_video, exit_loop],
    output_key="RENDER_ERROR",
)

# Loop: code → render → if error, re-code → re-render (max 5 iterations)
code_and_render_loop = LoopAgent(
    name="code_and_render_loop",
    description="Iteratively generates and renders Manim code, retrying on errors.",
    sub_agents=[manim_coder, renderer_agent],
    max_iterations=5,
)

# Full pipeline: research → script → code+render loop
root_agent = SequentialAgent(
    name="orchestrator",
    description="SketchMind: creates educational animated videos.",
    sub_agents=[researcher, scriptwriter, code_and_render_loop],
)
