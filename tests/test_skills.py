"""技能注册冒烟测试（不触发 LLM 调用，无需安装 litellm / instructor）。"""

from core.skill import skill_registry
from skills.user_extractor import UserExtractorSkill, UserInput, UserOutput


def test_user_extractor_is_registered():
    cls = skill_registry.get("user_extractor")
    assert cls is UserExtractorSkill
    assert cls.input_schema is UserInput
    assert cls.output_schema is UserOutput
    assert "circuit_breaker" in cls.middleware_names
    assert "contract" in cls.middleware_names


def test_schemas_shape():
    assert UserInput.model_fields["text"].is_required()
    assert UserOutput.model_fields["name"].is_required()
    assert UserOutput.model_fields["email"].is_required()
    assert UserOutput.model_fields["age"].default is None
