# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import logging
import re
from collections.abc import AsyncGenerator
from typing import Literal

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps.app import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.events import Event, EventActions
from google.adk.memory import VertexAiMemoryBankService
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from .a2ui_utils import a2ui_callback
from .car_tools import (
    lookup_car_in_cardex,
    list_cardex_cars,
    record_car_spot,
    view_my_garage,
    view_leaderboard,
    submit_car_review,
    decode_vin_specifications,
    search_vehicle_specs_database,
    lookup_nhtsa_models_by_year,
    fetch_carapi_live_specs,
    fetch_car_image_and_provenance,
    identify_and_spot_car,
)
from .config import config


# --- Structured Output Models ---
class SearchQuery(BaseModel):
    """Model representing a specific search query for web search."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class Feedback(BaseModel):
    """Model for providing evaluation feedback on research quality."""

    grade: Literal["pass", "fail"] = Field(
        description="Evaluation result. 'pass' if the research is sufficient, 'fail' if it needs revision."
    )
    comment: str = Field(
        description="Detailed explanation of the evaluation, highlighting strengths and/or weaknesses of the research."
    )
    follow_up_queries: list[SearchQuery] | None = Field(
        default=None,
        description="A list of specific, targeted follow-up search queries needed to fix research gaps. This should be null or empty if the grade is 'pass'.",
    )


# --- Callbacks ---
def collect_research_sources_callback(
    callback_context: CallbackContext,
) -> None:
    """Collects and organizes web-based research sources and their supported claims from agent events.

    This function processes the agent's `session.events` to extract web source details (URLs,
    titles, domains from `grounding_chunks`) and associated text segments with confidence scores
    (from `grounding_supports`). The aggregated source information and a mapping of URLs to short
    IDs are cumulatively stored in `callback_context.state`.

    Args:
        callback_context (CallbackContext): The context object providing access to the agent's
            session events and persistent state.
    """
    session = callback_context._invocation_context.session
    url_to_short_id = callback_context.state.get("url_to_short_id", {})
    sources = callback_context.state.get("sources", {})
    id_counter = len(url_to_short_id) + 1
    for event in session.events:
        if not (
            event.grounding_metadata
            and event.grounding_metadata.grounding_chunks
        ):
            continue
        chunks_info = {}
        for idx, chunk in enumerate(event.grounding_metadata.grounding_chunks):
            if not chunk.web:
                continue
            url = chunk.web.uri
            title = (
                chunk.web.title
                if chunk.web.title != chunk.web.domain
                else chunk.web.domain
            )
            if url not in url_to_short_id:
                short_id = f"src-{id_counter}"
                url_to_short_id[url] = short_id
                sources[short_id] = {
                    "short_id": short_id,
                    "title": title,
                    "url": url,
                    "domain": chunk.web.domain,
                    "supported_claims": [],
                }
                id_counter += 1
            chunks_info[idx] = url_to_short_id[url]
        if event.grounding_metadata.grounding_supports:
            for support in event.grounding_metadata.grounding_supports:
                confidence_scores = support.confidence_scores or []
                chunk_indices = support.grounding_chunk_indices or []
                for i, chunk_idx in enumerate(chunk_indices):
                    if chunk_idx in chunks_info:
                        short_id = chunks_info[chunk_idx]
                        confidence = (
                            confidence_scores[i]
                            if i < len(confidence_scores)
                            else 0.5
                        )
                        text_segment = (
                            support.segment.text if support.segment else ""
                        )
                        sources[short_id]["supported_claims"].append(
                            {
                                "text_segment": text_segment,
                                "confidence": confidence,
                            }
                        )
    callback_context.state["url_to_short_id"] = url_to_short_id
    callback_context.state["sources"] = sources


def citation_replacement_callback(
    callback_context: CallbackContext,
) -> genai_types.Content:
    """Replaces citation tags in a report with Markdown-formatted links.

    Processes 'final_cited_report' from context state, converting tags like
    `<cite source="src-N"/>` into hyperlinks using source information from
    `callback_context.state["sources"]`. Also fixes spacing around punctuation.

    Args:
        callback_context (CallbackContext): Contains the report and source information.

    Returns:
        genai_types.Content: The processed report with Markdown citation links.
    """
    final_report = callback_context.state.get("final_cited_report", "")
    sources = callback_context.state.get("sources", {})

    def tag_replacer(match: re.Match) -> str:
        short_id = match.group(1)
        if not (source_info := sources.get(short_id)):
            logging.warning(
                f"Invalid citation tag found and removed: {match.group(0)}"
            )
            return ""
        display_text = source_info.get(
            "title", source_info.get("domain", short_id)
        )
        return f" [{display_text}]({source_info['url']})"

    processed_report = re.sub(
        r'<cite\s+source\s*=\s*["\']?\s*(src-\d+)\s*["\']?\s*/>',
        tag_replacer,
        final_report,
    )
    processed_report = re.sub(r"\s+([.,;:])", r"\1", processed_report)
    callback_context.state["final_report_with_citations"] = processed_report
    return genai_types.Content(parts=[genai_types.Part(text=processed_report)])


# --- Custom Agent for Loop Control ---
class EscalationChecker(BaseAgent):
    """Checks research evaluation and escalates to stop the loop if grade is 'pass'."""

    def __init__(self, name: str):
        super().__init__(name=name)

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        evaluation_result = ctx.session.state.get("research_evaluation")
        if evaluation_result and evaluation_result.get("grade") == "pass":
            logging.info(
                f"[{self.name}] Research evaluation passed. Escalating to stop loop."
            )
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            logging.info(
                f"[{self.name}] Research evaluation failed or not found. Loop will continue."
            )
            # Yielding an event without content or actions just lets the flow continue.
            yield Event(author=self.name)


# --- AGENT DEFINITIONS ---
plan_generator = LlmAgent(
    model=config.worker_model,
    name="plan_generator",
    description="Generates or refine the existing 5 line action-oriented research plan, using minimal search only for topic clarification.",
    instruction=f"""
    You are a research strategist. Your job is to create a high-level RESEARCH PLAN, not a summary. If there is already a RESEARCH PLAN in the session state,
    improve upon it based on the user feedback.

    RESEARCH PLAN(SO FAR):
    {{ research_plan? }}

    **GENERAL INSTRUCTION: CLASSIFY TASK TYPES**
    Your plan must clearly classify each goal for downstream execution. Each bullet point should start with a task type prefix:
    - **`[RESEARCH]`**: For goals that primarily involve information gathering, investigation, analysis, or data collection (these require search tool usage by a researcher).
    - **`[DELIVERABLE]`**: For goals that involve synthesizing collected information, creating structured outputs (e.g., tables, charts, summaries, reports), or compiling final output artifacts (these are executed AFTER research tasks, often without further search).

    **INITIAL RULE: Your initial output MUST start with a bulleted list of 5 action-oriented research goals or key questions, followed by any *inherently implied* deliverables.**
    - All initial 5 goals will be classified as `[RESEARCH]` tasks.
    - A good goal for `[RESEARCH]` starts with a verb like "Analyze," "Identify," "Investigate."
    - A bad output is a statement of fact like "The event was in April 2024."
    - **Proactive Implied Deliverables (Initial):** If any of your initial 5 `[RESEARCH]` goals inherently imply a standard output or deliverable (e.g., a comparative analysis suggesting a comparison table, or a comprehensive review suggesting a summary document), you MUST add these as additional, distinct goals immediately after the initial 5. Phrase these as *synthesis or output creation actions* (e.g., "Create a summary," "Develop a comparison," "Compile a report") and prefix them with `[DELIVERABLE][IMPLIED]`.

    **REFINEMENT RULE**:
    - **Integrate Feedback & Mark Changes:** When incorporating user feedback, make targeted modifications to existing bullet points. Add `[MODIFIED]` to the existing task type and status prefix (e.g., `[RESEARCH][MODIFIED]`). If the feedback introduces new goals:
        - If it's an information gathering task, prefix it with `[RESEARCH][NEW]`.
        - If it's a synthesis or output creation task, prefix it with `[DELIVERABLE][NEW]`.
    - **Proactive Implied Deliverables (Refinement):** Beyond explicit user feedback, if the nature of an existing `[RESEARCH]` goal (e.g., requiring a structured comparison, deep dive analysis, or broad synthesis) or a `[DELIVERABLE]` goal inherently implies an additional, standard output or synthesis step (e.g., a detailed report following a summary, or a visual representation of complex data), proactively add this as a new goal. Phrase these as *synthesis or output creation actions* and prefix them with `[DELIVERABLE][IMPLIED]`.
    - **Maintain Order:** Strictly maintain the original sequential order of existing bullet points. New bullets, whether `[NEW]` or `[IMPLIED]`, should generally be appended to the list, unless the user explicitly instructs a specific insertion point.
    - **Flexible Length:** The refined plan is no longer constrained by the initial 5-bullet limit and may comprise more goals as needed to fully address the feedback and implied deliverables.

    **TOOL USE IS STRICTLY LIMITED:**
    Your goal is to create a generic, high-quality plan *without searching*.
    Only use `google_search` if a topic is ambiguous or time-sensitive and you absolutely cannot create a plan without a key piece of identifying information.
    You are explicitly forbidden from researching the *content* or *themes* of the topic. That is the next agent's job. Your search is only to identify the subject, not to investigate it.
    Current date: {datetime.datetime.now().strftime("%Y-%m-%d")}
    """,
    tools=[google_search],
)


section_planner = LlmAgent(
    model=config.worker_model,
    name="section_planner",
    description="Breaks down the research plan into a structured markdown outline of report sections.",
    instruction="""
    You are an expert report architect. Using the research topic and the plan from the 'research_plan' state key, design a logical structure for the final report.
    Note: Ignore all the tag nanes ([MODIFIED], [NEW], [RESEARCH], [DELIVERABLE]) in the research plan.
    Your task is to create a markdown outline with 4-6 distinct sections that cover the topic comprehensively without overlap.
    You can use any markdown format you prefer, but here's a suggested structure:
    # Section Name
    A brief overview of what this section covers
    Feel free to add subsections or bullet points if needed to better organize the content.
    Make sure your outline is clear and easy to follow.
    Do not include a "References" or "Sources" section in your outline. Citations will be handled in-line.
    """,
    output_key="report_sections",
)


section_researcher = LlmAgent(
    model=config.worker_model,
    name="section_researcher",
    description="Performs the crucial first pass of web research.",
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(include_thoughts=True)
    ),
    instruction="""
    You are a highly capable and diligent research and synthesis agent. Your comprehensive task is to execute a provided research plan with **absolute fidelity**, first by gathering necessary information, and then by synthesizing that information into specified outputs.

    You will be provided with a sequential list of research plan goals, stored in the `research_plan` state key. Each goal will be clearly prefixed with its primary task type: `[RESEARCH]` or `[DELIVERABLE]`.

    Your execution process must strictly adhere to these two distinct and sequential phases:

    ---

    **Phase 1: Information Gathering (`[RESEARCH]` Tasks)**

    *   **Execution Directive:** You **MUST** systematically process every goal prefixed with `[RESEARCH]` before proceeding to Phase 2.
    *   For each `[RESEARCH]` goal:
        *   **Query Generation:** Formulate a comprehensive set of 4-5 targeted search queries. These queries must be expertly designed to broadly cover the specific intent of the `[RESEARCH]` goal from multiple angles.
        *   **Execution:** Utilize the `google_search` tool to execute **all** generated queries for the current `[RESEARCH]` goal.
        *   **Summarization:** Synthesize the search results into a detailed, coherent summary that directly addresses the objective of the `[RESEARCH]` goal.
        *   **Internal Storage:** Store this summary, clearly tagged or indexed by its corresponding `[RESEARCH]` goal, for later and exclusive use in Phase 2. You **MUST NOT** lose or discard any generated summaries.

    ---

    **Phase 2: Synthesis and Output Creation (`[DELIVERABLE]` Tasks)**

    *   **Execution Prerequisite:** This phase **MUST ONLY COMMENCE** once **ALL** `[RESEARCH]` goals from Phase 1 have been fully completed and their summaries are internally stored.
    *   **Execution Directive:** You **MUST** systematically process **every** goal prefixed with `[DELIVERABLE]`. For each `[DELIVERABLE]` goal, your directive is to **PRODUCE** the artifact as explicitly described.
    *   For each `[DELIVERABLE]` goal:
        *   **Instruction Interpretation:** You will interpret the goal's text (following the `[DELIVERABLE]` tag) as a **direct and non-negotiable instruction** to generate a specific output artifact.
            *   *If the instruction details a table (e.g., "Create a Detailed Comparison Table in Markdown format"), your output for this step **MUST** be a properly formatted Markdown table utilizing columns and rows as implied by the instruction and the prepared data.*
            *   *If the instruction states to prepare a summary, report, or any other structured output, your output for this step **MUST** be that precise artifact.*
        *   **Data Consolidation:** Access and utilize **ONLY** the summaries generated during Phase 1 (`[RESEARCH]` tasks`) to fulfill the requirements of the current `[DELIVERABLE]` goal. You **MUST NOT** perform new searches.
        *   **Output Generation:** Based on the specific instruction of the `[DELIVERABLE]` goal:
            *   Carefully extract, organize, and synthesize the relevant information from your previously gathered summaries.
            *   Must always produce the specified output artifact (e.g., a concise summary, a structured comparison table, a comprehensive report, a visual representation, etc.) with accuracy and completeness.
        *   **Output Accumulation:** Maintain and accumulate **all** the generated `[DELIVERABLE]` artifacts. These are your final outputs.

    ---

    **Final Output:** Your final output will comprise the complete set of processed summaries from `[RESEARCH]` tasks AND all the generated artifacts from `[DELIVERABLE]` tasks, presented clearly and distinctly.
    """,
    tools=[google_search],
    output_key="section_research_findings",
    after_agent_callback=collect_research_sources_callback,
)

research_evaluator = LlmAgent(
    model=config.critic_model,
    name="research_evaluator",
    description="Critically evaluates research and generates follow-up queries.",
    instruction=f"""
    You are a meticulous quality assurance analyst evaluating the research findings in 'section_research_findings'.

    **CRITICAL RULES:**
    1. Assume the given research topic is correct. Do not question or try to verify the subject itself.
    2. Your ONLY job is to assess the quality, depth, and completeness of the research provided *for that topic*.
    3. Focus on evaluating: Comprehensiveness of coverage, logical flow and organization, use of credible sources, depth of analysis, and clarity of explanations.
    4. Do NOT fact-check or question the fundamental premise or timeline of the topic.
    5. If suggesting follow-up queries, they should dive deeper into the existing topic, not question its validity.

    Be very critical about the QUALITY of research. If you find significant gaps in depth or coverage, assign a grade of "fail",
    write a detailed comment about what's missing, and generate 5-7 specific follow-up queries to fill those gaps.
    If the research thoroughly covers the topic, grade "pass".

    Current date: {datetime.datetime.now().strftime("%Y-%m-%d")}
    Your response must be a single, raw JSON object validating against the 'Feedback' schema.
    """,
    output_schema=Feedback,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_key="research_evaluation",
)

enhanced_search_executor = LlmAgent(
    model=config.worker_model,
    name="enhanced_search_executor",
    description="Executes follow-up searches and integrates new findings.",
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(include_thoughts=True)
    ),
    instruction="""
    You are a specialist researcher executing a refinement pass.
    You have been activated because the previous research was graded as 'fail'.

    1.  Review the 'research_evaluation' state key to understand the feedback and required fixes.
    2.  Execute EVERY query listed in 'follow_up_queries' using the 'google_search' tool.
    3.  Synthesize the new findings and COMBINE them with the existing information in 'section_research_findings'.
    4.  Your output MUST be the new, complete, and improved set of research findings.
    """,
    tools=[google_search],
    output_key="section_research_findings",
    after_agent_callback=collect_research_sources_callback,
)

report_composer = LlmAgent(
    model=config.critic_model,
    name="report_composer_with_citations",
    include_contents="none",
    description="Transforms research data and a markdown outline into a final, cited report.",
    instruction="""
    Transform the provided data into a polished, professional, and meticulously cited research report.

    ---
    ### INPUT DATA
    *   Research Plan: `{research_plan}`
    *   Research Findings: `{section_research_findings}`
    *   Citation Sources: `{sources}`
    *   Report Structure: `{report_sections}`

    ---
    ### CRITICAL: Citation System
    To cite a source, you MUST insert a special citation tag directly after the claim it supports.

    **The only correct format is:** `<cite source="src-ID_NUMBER" />`

    ---
    ### Final Instructions
    Generate a comprehensive report using ONLY the `<cite source="src-ID_NUMBER" />` tag system for all citations.
    The final report must strictly follow the structure provided in the **Report Structure** markdown outline.
    Do not include a "References" or "Sources" section; all citations must be in-line.
    """,
    output_key="final_cited_report",
    after_agent_callback=citation_replacement_callback,
)

research_pipeline = SequentialAgent(
    name="research_pipeline",
    description="Executes a pre-approved research plan. It performs iterative research, evaluation, and composes a final, cited report.",
    sub_agents=[
        section_planner,
        section_researcher,
        LoopAgent(
            name="iterative_refinement_loop",
            max_iterations=config.max_search_iterations,
            sub_agents=[
                research_evaluator,
                EscalationChecker(name="escalation_checker"),
                enhanced_search_executor,
            ],
        ),
        report_composer,
    ],
)

# Agent Engine Sandbox & Memory Bank Configuration
AGENT_ENGINE_ID = "1778936146462507008"
AGENT_ENGINE_RESOURCE = "projects/861697384142/locations/us-east1/reasoningEngines/1778936146462507008"
SANDBOX_RESOURCE = "projects/861697384142/locations/us-east1/reasoningEngines/1778936146462507008/sandboxEnvironments/7730613478334922752"

# Agent Platform Code Execution in Sandbox Environment
agent_sandbox_executor = AgentEngineSandboxCodeExecutor(
    sandbox_resource_name=SANDBOX_RESOURCE,
    agent_engine_resource_name=AGENT_ENGINE_RESOURCE,
)

# Cross-Session Long-Term Memory Callback (Vertex AI Memory Bank)
async def generate_memories_callback(callback_context: CallbackContext):
    """Saves session turns to Vertex AI Memory Bank for long-term user recall."""
    await callback_context.add_session_to_memory()
    return None


# A2UI Schema Manager for rich visual cards (0.8)
a2ui_schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

cardex_role_description = f"""
You are CarDex, the intelligent Car Spotting and Vehicle Knowledge Assistant (Pokémon Go / Pokédex for cars!).

Current date: {datetime.datetime.now().strftime("%Y-%m-%d")}

Your mission is to help car enthusiasts identify vehicles, track sightings, build their personal garage, earn points based on vehicle rarity, check global leaderboards, and handle reviews or dispute submissions.

Capabilities & Tools:
1. **Car Spotting & Garage Logging**:
   - When a user spots a car (or describes finding one in public), use `record_car_spot` to log it to their garage.
   - Announce the points awarded, the vehicle's rarity tier (Mythic 1-of-1, Legendary, Epic, Rare, Uncommon, Common), and the updated Shazam-style global spotted count!

2. **Vehicle Database & Pokédex Lookups**:
   - When a user asks about any vehicle, special edition, 1-of-1 hypercar, specs, or production numbers, use `lookup_car_in_cardex`.
   - If a car is not found in the local CarDex database, use `google_search` to find real-world specifications, rarity, and production numbers.
   - Use `list_cardex_cars` when users want to browse cars by rarity tier or manufacturer.

3. **Garage & Collections**:
   - When a user asks to see what they have collected or their garage status, call `view_my_garage`.

4. **AI Multimodal Vision Identification & Spotting (Gemini in Global Region)**:
   - When a user sends an image, photo link, or base64 image data of a car spotted out in public, call `identify_and_spot_car`.
   - It automatically uploads the image to the public Cloud Storage bucket in memory (never writes local files), analyzes it using Gemini multimodal vision in the global region to determine make, model, trim, colorway, and special edition status, cross-references CarDex, and logs the spot to award points.
   - Always return the public HTTPS URL from Cloud Storage in your response so the user can see their uploaded spot photo embedded.

5. **Spotter Leaderboards**:
   - When a user asks who is leading, who the best spotter is, or asks about rankings, call `view_leaderboard`.

6. **Review & Disputes (Dispute / Correction Button)**:
   - If a user reports that a car identification was wrong, a trim was misclassified, or wants to submit an unlisted rare build for review, use `submit_car_review` to record their review request in Firestore.

7. **Official Federal NHTSA VIN & Models Registry (100% Free & Uncapped)**:
   - `decode_vin_specifications`: Call this when a spotter shares a 17-digit VIN to extract official plant country, engine, displacement, horsepower, and trim series.
   - `lookup_nhtsa_models_by_year`: Call this to query the official federal database for all models registered by a manufacturer in a specific model year (1980 through 2026/2027).

8. **Comprehensive Vehicle Specs Database (164 Makes, 2,649 Models)**:
   - When a user asks about the technical specs, horsepower, top speed (km/h), 0-100 acceleration, torque, transmission, or engine of any production or enthusiast car, call `search_vehicle_specs_database`.

9. **Live CarAPI Trims & OEM Paint Colors**:
   - When a spotter wants real dealer MSRP pricing, trims, or factory exterior color names with exact RGB color codes, call `fetch_carapi_live_specs`.

10. **Verified Vehicle Photos & Provenance**:
    - When a spotter wants to view real verified photos of a car or needs historical provenance, call `fetch_car_image_and_provenance` to get high-resolution images and summary dossier.

11. **Deep Automotive Research & Web Verification**:
    - For rare bespoke 1-of-1s, auction results (Bring a Trailer, RM Sotheby's), or broad research, you have access to `google_search` and `plan_generator` / `research_pipeline`.

12. **Cross-Session Long-Term Memory (Vertex AI Memory Bank)**:
    - You remember the user's stated preferences, favorite car brands, spotter locations, dream cars, unit preferences (km/h vs mph), and facts from previous conversations and use them to personalize your responses.
    - CRITICAL: Always remember ALL user allergies (dietary, food, medical, chemical, material, or environmental allergies and health sensitivities). Whenever a user mentions any allergy or sensitivity, explicitly acknowledge it and ensure it is remembered across all sessions.

13. **Agent Platform Code Sandbox Execution**:
    - You can write and execute Python code in your secure Agent Engine sandbox whenever you need to compute complex rarity scoring algorithms, batch statistics, performance conversions, or analyze automotive calculations.

Always be enthusiastic about cars, automotive engineering, rarity, and spotter achievements!
"""

cardex_instruction = a2ui_schema_manager.generate_system_prompt(
    role_description=cardex_role_description,
    workflow_description="Analyze the request and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)

cardex_agent = LlmAgent(
    name="cardex_agent",
    model=config.worker_model,
    description="CarDex AI Assistant: The ultimate car spotting, vehicle identification, Pokédex-style garage tracker, and leaderboard assistant.",
    instruction=cardex_instruction,
    sub_agents=[research_pipeline],
    code_executor=agent_sandbox_executor,
    after_model_callback=a2ui_callback,
    after_agent_callback=generate_memories_callback,
    tools=[
        PreloadMemoryTool(),
        identify_and_spot_car,
        lookup_car_in_cardex,
        list_cardex_cars,
        record_car_spot,
        view_my_garage,
        view_leaderboard,
        submit_car_review,
        decode_vin_specifications,
        lookup_nhtsa_models_by_year,
        search_vehicle_specs_database,
        fetch_carapi_live_specs,
        fetch_car_image_and_provenance,
        AgentTool(plan_generator),
    ],
)

def memory_bank_service_builder():
    return VertexAiMemoryBankService(
        project="qwiklabs-gcp-04-6f324b699fdd",
        location="us-east1",
        agent_engine_id=AGENT_ENGINE_ID,
    )

root_agent = cardex_agent
app = App(
    root_agent=root_agent,
    name="app",
)
