from google.adk.agents import Agent
from google.adk.tools import google_search
from tools.render_tool import render_manim_video

researcher = Agent(
    name="researcher",
    model="gemini-2.5-flash",
    description="Researches topics using web search.",
    instruction="""Research specialist. Use Google Search for accurate info.
    Output a structured brief (max 400 words) with core concepts,
    definitions, formulas, and examples.""",
    tools=[google_search],
)

scriptwriter = Agent(
    name="scriptwriter",
    model="gemini-2.5-flash",
    description="Creates video scripts as JSON.",
    instruction="""Output ONLY a JSON array:
    [{"scene_number":1,"audio_script":"...","visual_directive":"...","duration_seconds":10}]
    RULES: 4-6 scenes, 60-90 sec total. visual_directive: ONLY Manim-drawable
    (Axes, graphs, Circle, Rectangle, Arrow, Text, MathTex). No images.
    Start with title, end with summary. Output ONLY valid JSON.""",
)

manim_coder = Agent(
    name="manim_coder",
    model="gemini-2.5-flash",
    description="Generates Manim Community Edition Python code.",
    instruction="""Output ONLY raw Python code. No markdown.
    RULES: from manim import * | Class = GeneratedScene |
    manimce syntax only | No ImageMobject/SVGMobject |
    Use Create not ShowCreation | color= in constructor not .set_color()
    If you get an error, fix only that error and return full corrected code.""",
)

root_agent = Agent(
    name="orchestrator",
    model="gemini-2.5-flash",
    instruction="""You are SketchMind. Create educational animated videos.
    WORKFLOW: 1) Delegate to 'researcher' 2) Pass research to 'scriptwriter'
    3) Pass JSON to 'manim_coder' 4) Call render_manim_video with the code
    5) If render fails, send error to manim_coder, retry (max 2 retries)
    6) Return the video_url.""",
    sub_agents=[researcher, scriptwriter, manim_coder],
    tools=[render_manim_video],
)
