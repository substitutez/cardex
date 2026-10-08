import pytest
from app.agent import (
    root_agent,
    cardex_agent,
    research_pipeline,
    plan_generator,
    section_planner,
    section_researcher,
    research_evaluator,
    enhanced_search_executor,
    report_composer,
    EscalationChecker,
)
from google.adk.agents import LoopAgent, SequentialAgent


def test_root_agent_structure():
    assert root_agent.name == "cardex_agent"
    assert len(root_agent.sub_agents) == 1
    assert root_agent.sub_agents[0].name == "research_pipeline"
    tool_names = [getattr(t, "name", None) or getattr(t, "__name__", None) for t in root_agent.tools]
    assert "preload_memory" in tool_names
    assert "identify_and_spot_car" in tool_names
    assert root_agent.code_executor is not None
    assert root_agent.after_agent_callback is not None
    assert root_agent.after_model_callback is not None
    assert "allergies" in root_agent.instruction
    assert "<a2ui-json>" in root_agent.instruction
    assert "lookup_car_in_cardex" in tool_names
    assert "list_cardex_cars" in tool_names
    assert "record_car_spot" in tool_names
    assert "view_my_garage" in tool_names
    assert "view_leaderboard" in tool_names
    assert "submit_car_review" in tool_names
    assert "decode_vin_specifications" in tool_names
    assert "lookup_nhtsa_models_by_year" in tool_names
    assert "search_vehicle_specs_database" in tool_names
    assert "fetch_carapi_live_specs" in tool_names
    assert "fetch_car_image_and_provenance" in tool_names
    assert "plan_generator" in tool_names


def test_research_pipeline_subagents():
    assert isinstance(research_pipeline, SequentialAgent)
    subagent_names = [a.name for a in research_pipeline.sub_agents]
    assert subagent_names == [
        "section_planner",
        "section_researcher",
        "iterative_refinement_loop",
        "report_composer_with_citations",
    ]


def test_refinement_loop_agents():
    loop = next(a for a in research_pipeline.sub_agents if a.name == "iterative_refinement_loop")
    assert isinstance(loop, LoopAgent)
    loop_subagent_names = [a.name for a in loop.sub_agents]
    assert loop_subagent_names == [
        "research_evaluator",
        "escalation_checker",
        "enhanced_search_executor",
    ]
