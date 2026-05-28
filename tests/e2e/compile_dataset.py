"""
真实 E2E 数据集编译器

把人类可读的 YAML/txt 源文件编译成 runner 需要的 JSON 数据集。
runner 架构完全不变，仍然读 JSON。

用法:
    python tests/e2e/compile_dataset.py \
        tests/e2e/real_datasets_src/alignment-classifier-popup-real.txt

输出到:
    tests/e2e/real_datasets/alignment-classifier-popup-real.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import yaml


def _first_user_message(case: dict) -> str:
    turns = case.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if isinstance(turn, dict) and turn.get("role", "user") == "user":
                return str(turn.get("message", ""))
    input_data = case.get("input")
    if isinstance(input_data, dict):
        return str(input_data.get("message", ""))
    return ""


def load_dataset_source(src_path: Path) -> dict:
    """读取并校验数据集源文件。"""
    src_path = src_path.resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"源文件不存在: {src_path}")

    raw = yaml.safe_load(src_path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise ValueError("源文件顶层必须是 mapping")
    if "suite_id" not in raw:
        raise ValueError("源文件缺少必填字段: suite_id")
    if not isinstance(raw.get("cases"), list):
        raise ValueError("源文件 cases 必须是 list")

    return raw


def build_dataset(raw: dict) -> dict:
    """把源文件结构转换为 runner 需要的 JSON 数据集结构。"""
    raw = copy.deepcopy(raw)
    # 取出 defaults anchor（YAML merge key 已在解析时展开，这里只需丢弃顶层 defaults）
    raw.pop("defaults", None)

    # 把 cases 里的扁平字段重新组装为标准 JSON 结构
    cases: list[dict] = []
    for index, case in enumerate(raw["cases"], start=1):
        if not isinstance(case, dict):
            raise ValueError(f"cases[{index}] 必须是 mapping")
        if "id" not in case:
            raise ValueError(f"cases[{index}] 缺少必填字段: id")

        compiled_case: dict = {"id": case["id"]}

        # 基础字段。txt/YAML 源文件是评测控制台的编辑事实源,这些字段
        # 虽不一定参与 runner 判定,但会进入证据包和人工复盘上下文。
        for key in ("title", "description", "source", "tags", "tool_chain", "evidence_required", "severity"):
            if key in case:
                compiled_case[key] = case[key]

        # setup —— 必须深拷贝，因为 YAML anchor 可能导致多个 case 共享同一个 dict
        setup = copy.deepcopy(case.get("setup", {}))
        for k in ("create_learning_unit", "frontend_mode", "session_per_case",
                  "seed_text", "manual_only", "manual_only_reason", "env_overrides"):
            if k in case:
                setup[k] = case[k]
        if setup:
            compiled_case["setup"] = setup

        # turns
        turns = case.get("turns", [])
        if turns:
            compiled_case["turns"] = turns

        # input.message: legacy runner compatibility. New txt/YAML sources can
        # use turns, but the web-search runner still consumes input.message.
        input_data = copy.deepcopy(case.get("input", {}))
        if not isinstance(input_data, dict):
            input_data = {}
        first_user_message = _first_user_message(case)
        if first_user_message:
            input_data["message"] = first_user_message
        if input_data:
            compiled_case["input"] = input_data

        # expect / expect_per_turn / forbid
        for key in ("expect", "expect_per_turn", "forbid"):
            if key in case:
                compiled_case[key] = case[key]

        cases.append(compiled_case)

    # 组装最终 JSON
    return {
        "suite_id": raw["suite_id"],
        "description": raw.get("description", ""),
        "owner": raw.get("owner", ""),
        "dataset_version": raw.get("dataset_version", ""),
        "execution_mode": raw.get("execution_mode", "manual_real_environment"),
        "target_surface": raw.get("target_surface", "frontend_or_api"),
        "linked_change": raw.get("linked_change", ""),
        "linked_refactor_note": raw.get("linked_refactor_note", ""),
        "runner_requirements": raw.get("runner_requirements", {}),
        "acceptance_metrics": raw.get("acceptance_metrics", {}),
        "session_plan": raw.get("session_plan", {}),
        "global_expectations": raw.get("global_expectations", {}),
        "cases": cases,
    }


def compile_dataset(src_path: Path, out_dir: Path | None = None) -> Path:
    """编译单个数据集源文件为 JSON。"""
    src_path = src_path.resolve()
    dataset = build_dataset(load_dataset_source(src_path))

    # 输出路径
    if out_dir is None:
        out_dir = src_path.parent.parent / "real_datasets"
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_name = src_path.stem + ".json"
    out_path = out_dir / out_name

    out_path.write_text(
        json.dumps(dataset, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="编译 E2E 数据集源文件为 JSON")
    parser.add_argument("src", type=Path, help="YAML/txt 源文件路径")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="JSON 输出目录（默认: tests/e2e/real_datasets/）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅校验源文件格式，不写出",
    )
    args = parser.parse_args()

    try:
        if args.check:
            build_dataset(load_dataset_source(args.src))
            print(f"[check] 格式校验通过: {args.src}")
            return 0

        out_path = compile_dataset(args.src, out_dir=args.out_dir)
        print(f"编译完成: {out_path}")
        # 快速统计
        data = json.loads(out_path.read_text(encoding="utf-8"))
        print(f"  suite: {data['suite_id']}")
        print(f"  cases: {len(data['cases'])}")
        return 0
    except Exception as exc:
        print(f"编译失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
