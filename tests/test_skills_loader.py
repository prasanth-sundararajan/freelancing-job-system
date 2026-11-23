from freelance_agents.skills_loader import AGENT_KEYS, Skills


def test_every_agent_has_a_full_rtcfr_section(cfg):
    skills = Skills.load(cfg["skills_file"])
    assert "global" in skills.sections
    for key in AGENT_KEYS.values():
        section = skills.sections[key]
        for part in ("### Role", "### Task", "### Guardrails", "### Context", "### Few-shots", "### Response"):
            assert part in section, f"{key} is missing {part}"


def test_prompt_has_global_rules_and_only_own_skill(cfg):
    prompt = Skills.load(cfg["skills_file"]).prompt_for("qualifier")
    assert "Global Rules" in prompt
    assert "Qualifier Agent" in prompt
    assert "Proposal Writer Agent" not in prompt
    assert "Config reference" not in prompt
