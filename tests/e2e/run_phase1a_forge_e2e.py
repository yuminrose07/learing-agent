#!/usr/bin/env python3
"""Phase 1A 铸造状态骨架 E2E 真实环境测试脚本。

针对 tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json
的 4 条用例，驱动真实服务器验证：
  - 研学卷创建并携带 forge_stage / temperature_state
  - SSE metadata 包含铸造字段
  - 首轮 STUDY 后 forge_stage 可推进为 collision
  - ASK 对齐轮不推进 forge_stage
"""

import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8899"
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"
WARN = "\033[93m[WARN]\033[0m"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()}


def stream_chat(session_id, message):
    """发送 SSE 流式聊天请求，返回 (full_text, metadata_list)。"""
    body = json.dumps({"message": message, "stream": True}).encode()
    req = urllib.request.Request(
        f"{BASE}/sessions/{session_id}/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    text_parts = []
    metadata_list = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        buffer = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                event_str, buffer = buffer.split("\n\n", 1)
                for line in event_str.split("\n"):
                    if line.startswith("data: "):
                        try:
                            payload = json.loads(line[6:])
                            if payload.get("content"):
                                text_parts.append(payload["content"])
                            metadata_list.append(payload)
                        except json.JSONDecodeError:
                            pass
    return "".join(text_parts), metadata_list


def stop_unit(unit_id):
    return api("POST", f"/learning-units/{unit_id}/stop", {"reason": "e2e_cleanup"})


def run_case(case):
    case_id = case["id"]
    title = case["title"]
    seed_text = case["setup"]["seed_text"]
    user_msg = case["turns"][0]["message"]
    expect = case["expect"]
    forbid = case.get("forbid", {})

    print(f"\n{'='*70}")
    print(f"  用例: {case_id}")
    print(f"  标题: {title}")
    print(f"  种子: {seed_text}")
    print(f"{'='*70}")

    results = []

    # 1) 创建研学卷
    create_res = api("POST", "/learning-units", {"seed_text": seed_text, "source": "user_written"})
    if "_error" in create_res:
        print(f"  {FAIL} 创建研学卷失败: {create_res}")
        return False
    unit_id = create_res["id"]
    session_id = create_res["session_id"]
    print(f"  {INFO} 研学卷: {unit_id}  会话: {session_id}")

    # 检查创建时的 forge_stage
    ok = create_res.get("forge_stage") == "entry"
    results.append(ok)
    print(f"  {'  '+PASS if ok else '  '+FAIL} 创建时 forge_stage=entry → {create_res.get('forge_stage')}")

    ok = create_res.get("temperature_state") == "steady"
    results.append(ok)
    print(f"  {'  '+PASS if ok else '  '+FAIL} 创建时 temperature_state=steady → {create_res.get('temperature_state')}")

    # 2) 发送 SSE 聊天
    print(f"  {INFO} 发送消息: {user_msg[:40]}...")
    full_text, metadata_list = stream_chat(session_id, user_msg)

    # 检查非空响应
    non_empty = bool(full_text.strip())
    results.append(non_empty)
    print(f"  {'  '+PASS if non_empty else '  '+FAIL} 非空响应 ({len(full_text)} 字)")

    if forbid.get("contains"):
        for banned in forbid["contains"]:
            found = banned in full_text
            ok = not found
            results.append(ok)
            print(f"  {'  '+PASS if ok else '  '+FAIL} 响应不含「{banned}」→ {'未找到' if ok else '包含!'}")

    # 检查 SSE metadata
    has_forge = any(m.get("forge_stage") for m in metadata_list)
    has_temp = any(m.get("temperature_state") for m in metadata_list)
    has_action = any(m.get("learning_action") for m in metadata_list)

    results.append(has_forge)
    print(f"  {'  '+PASS if has_forge else '  '+FAIL} SSE metadata 含 forge_stage")

    results.append(has_temp)
    print(f"  {'  '+PASS if has_temp else '  '+FAIL} SSE metadata 含 temperature_state")

    # 提取 SSE 中的 mode/forge/action
    modes_seen = set()
    forge_stages_in_sse = set()
    actions_in_sse = set()
    for m in metadata_list:
        if m.get("mode"):
            modes_seen.add(m["mode"])
        if m.get("forge_stage"):
            forge_stages_in_sse.add(m["forge_stage"])
        if m.get("learning_action"):
            actions_in_sse.add(m["learning_action"])

    print(f"  {INFO} SSE modes: {modes_seen}  forge_stages: {forge_stages_in_sse}  actions: {actions_in_sse}")

    is_ask = "ask" in modes_seen

    if is_ask:
        print(f"  {WARN} 此轮触发了 ASK 对齐")
        # ASK 轮不应有 learning_action
        ok = not has_action
        results.append(ok)
        print(f"  {'  '+PASS if ok else '  '+FAIL} ASK 轮无 learning_action")
    else:
        # STUDY 轮应有 learning_action
        if expect.get("assistant_metadata_should_include") and "learning_action" in expect["assistant_metadata_should_include"]:
            results.append(has_action)
            print(f"  {'  '+PASS if has_action else '  '+FAIL} STUDY SSE metadata 含 learning_action")

    # 3) GET 研学卷最终状态
    unit = api("GET", f"/learning-units/{unit_id}")
    final_forge = unit.get("forge_stage")
    final_temp = unit.get("temperature_state")
    final_phase = unit.get("phase")
    print(f"  {INFO} GET 最终状态: phase={final_phase}  forge_stage={final_forge}  temperature_state={final_temp}")

    # 验证 allowed_forge_stage_after_turn
    allowed = expect.get("allowed_forge_stage_after_turn")
    if allowed:
        ok = final_forge in allowed
        results.append(ok)
        print(f"  {'  '+PASS if ok else '  '+FAIL} forge_stage={final_forge} ∈ {allowed}")

    allowed_temp = expect.get("allowed_temperature_state_after_turn")
    if allowed_temp:
        ok = final_temp in allowed_temp
        results.append(ok)
        print(f"  {'  '+PASS if ok else '  '+FAIL} temperature_state={final_temp} ∈ {allowed_temp}")

    # ASK 特殊检查
    if is_ask and expect.get("if_alignment_true_then_forge_stage_should_remain"):
        expected_stage = expect["if_alignment_true_then_forge_stage_should_remain"]
        ok = final_forge == expected_stage
        results.append(ok)
        print(f"  {'  '+PASS if ok else '  '+FAIL} ASK 后 forge_stage 仍为 {expected_stage} → {final_forge}")

    # 前端-后端一致性
    if expect.get("frontend_backend_consistent"):
        ok = "forge_stage" in unit and "temperature_state" in unit
        results.append(ok)
        print(f"  {'  '+PASS if ok else '  '+FAIL} GET 载荷含 forge_stage + temperature_state（前端可读）")

    # 4) 清理：停止研学卷
    stop_res = stop_unit(unit_id)
    stopped_ok = stop_res.get("phase") == "stopped"
    print(f"  {INFO} 清理: stop → phase={stop_res.get('phase')}")

    all_ok = all(results)
    verdict = PASS if all_ok else FAIL
    print(f"\n  {verdict} 用例 {case_id}: {len([r for r in results if r])}/{len(results)} 项通过")
    return all_ok


def main():
    with open("tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json") as f:
        dataset = json.load(f)

    print(f"{'='*70}")
    print(f"  Phase 1A 铸造状态骨架 E2E 测试")
    print(f"  数据集: {dataset['suite_id']} ({dataset['dataset_version']})")
    print(f"  用例数: {len(dataset['cases'])}")
    print(f"  服务器: {BASE}")
    print(f"{'='*70}")

    all_ok = True
    case_results = []
    for case in dataset["cases"]:
        ok = run_case(case)
        case_results.append((case["id"], ok))

    print(f"\n\n{'='*70}")
    print(f"  汇总")
    print(f"{'='*70}")
    for case_id, ok in case_results:
        print(f"  {PASS if ok else FAIL} {case_id}")
    passed = sum(1 for _, ok in case_results if ok)
    total = len(case_results)
    print(f"\n  总计: {passed}/{total} 用例通过")
    print(f"{'='*70}")

    return 0 if all(ok for _, ok in case_results) else 1


if __name__ == "__main__":
    sys.exit(main())
