"""sop-sync 双源同步测试：diff 三态、apply 推送与复核、方向约定。"""

import json

import pytest

import multica_qa_loop as qa_loop


def _setup_sources(tmp_path):
    src = tmp_path / "skill_sop"
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text("# SOP 主文档 v2\n", encoding="utf-8")
    (src / "references" / "a.md").write_text("规则 A", encoding="utf-8")
    (src / "references" / "b.md").write_text("规则 B", encoding="utf-8")
    return src


def _fake_multica(monkeypatch, remote_main: str, remote_files: dict, upserts: list):
    remote_main_ref = [remote_main]

    async def fake_run(*args: str) -> str:
        argv = list(args)
        if argv[:2] == ["skill", "get"]:
            return json.dumps({
                "content": remote_main_ref[0],
                "files": [{"path": p, "content": c} for p, c in remote_files.items()],
            })
        if argv[:3] == ["skill", "files", "upsert"]:
            path = argv[argv.index("--path") + 1]
            content_file = argv[argv.index("--content-file") + 1]
            content = open(content_file, encoding="utf-8").read()
            upserts.append({"path": path, "content": content})
            remote_files[path] = content
            return "{}"
        if argv[:2] == ["skill", "update"]:
            content_file = argv[argv.index("--content-file") + 1]
            new_main = open(content_file, encoding="utf-8").read()
            upserts.append({"path": "SKILL.md", "content": new_main})
            remote_main_ref[0] = new_main
            return "{}"
        return "{}"

    monkeypatch.setattr(qa_loop, "run_multica", fake_run)
    return remote_main_ref


async def test_sop_sync_reports_in_sync(tmp_path, monkeypatch):
    src = _setup_sources(tmp_path)
    monkeypatch.setattr(qa_loop, "SOP_SOURCE_DIR", src)
    _fake_multica(
        monkeypatch,
        remote_main="# SOP 主文档 v2\n",
        remote_files={"references/a.md": "规则 A", "references/b.md": "规则 B"},
        upserts=[],
    )

    result = await qa_loop.sop_sync_phase(apply=False)

    assert result["in_sync"] is True and result["drifts"] == []


async def test_sop_sync_detects_all_three_kinds(tmp_path, monkeypatch):
    src = _setup_sources(tmp_path)
    monkeypatch.setattr(qa_loop, "SOP_SOURCE_DIR", src)
    _fake_multica(
        monkeypatch,
        remote_main="# SOP 主文档 v1\n",  # 主文档漂移
        remote_files={"references/a.md": "规则 A 旧版", "references/removed.md": "本地已删"},  # changed + extra-remote；b.md missing
        upserts=[],
    )

    result = await qa_loop.sop_sync_phase(apply=False)

    kinds = {d["path"]: d["kind"] for d in result["drifts"]}
    assert kinds["SKILL.md"] == "changed"
    assert kinds["references/a.md"] == "changed"
    assert kinds["references/b.md"] == "missing"
    assert kinds["references/removed.md"] == "extra-remote"


async def test_sop_sync_apply_pushes_and_rechecks(tmp_path, monkeypatch):
    src = _setup_sources(tmp_path)
    monkeypatch.setattr(qa_loop, "SOP_SOURCE_DIR", src)
    upserts: list = []
    ref = _fake_multica(
        monkeypatch,
        remote_main="# 旧主文档\n",
        remote_files={"references/a.md": "规则 A 旧版"},
        upserts=upserts,
    )

    result = await qa_loop.sop_sync_phase(apply=True)

    assert result["applied"] == 3  # SKILL.md + a.md + b.md
    assert result["remaining"] == []  # 复核通过（extra-remote 未计入 remaining——只 apply 可同步项）
    pushed_paths = {u["path"] for u in upserts}
    assert pushed_paths == {"SKILL.md", "references/a.md", "references/b.md"}


async def test_sop_sync_apply_never_deletes_extra_remote(tmp_path, monkeypatch):
    src = _setup_sources(tmp_path)
    monkeypatch.setattr(qa_loop, "SOP_SOURCE_DIR", src)
    upserts: list = []
    _fake_multica(
        monkeypatch,
        remote_main="# SOP 主文档 v2\n",
        remote_files={"references/a.md": "规则 A", "references/b.md": "规则 B", "references/ghost.md": "幽灵"},
        upserts=upserts,
    )

    result = await qa_loop.sop_sync_phase(apply=True)

    assert any(u["path"] == "references/ghost.md" for u in upserts) is False  # 删除是破坏性操作，保持人工
    assert result["applied"] == 1  # extra-remote 计入 drift 但被跳过（未产生任何 upsert）
    assert upserts == []  # 实际零推送
