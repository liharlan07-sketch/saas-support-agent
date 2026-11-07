"""合成商家、店铺和订单。只对演示创建的临时数据库执行种子写入。"""

from contextlib import ExitStack, closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from saas_support_agent.core.models import TrustedIdentity
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.repositories.database import Database


DEMO_IDENTITY = TrustedIdentity("demo-merchant", "demo-workspace", "demo-session", "demo-request")


def seed_demo(database):
    with closing(database.connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO users VALUES (?, ?, 'active')", (DEMO_IDENTITY.user_id, "演示商家"))
        connection.execute("INSERT INTO workspaces VALUES (?, ?, 'active', 'basic', 1)",
                           (DEMO_IDENTITY.workspace_id, "演示工作空间"))
        connection.execute("INSERT INTO memberships VALUES (?, ?, ?, 'member', 'active', 1)",
                           ("demo-membership", DEMO_IDENTITY.workspace_id, DEMO_IDENTITY.user_id))
        connection.executemany("INSERT INTO shops VALUES (?, ?, ?, 'active', 1)", [
            (DEMO_IDENTITY.workspace_id, "SHOP-1", "演示可见店铺"),
            (DEMO_IDENTITY.workspace_id, "SHOP-2", "演示不可见店铺"),
        ])
        connection.execute("INSERT INTO shop_access VALUES (?, ?, ?, 1)",
                           (DEMO_IDENTITY.workspace_id, "SHOP-1", DEMO_IDENTITY.user_id))
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, 1)", [
            (DEMO_IDENTITY.workspace_id, "SHOP-1", "ORD-1001", "delivered", "2026-10-20"),
            (DEMO_IDENTITY.workspace_id, "SHOP-2", "ORD-2001", "shipped", None),
        ])
        database.commit(connection)


def render(result):
    print(f"[{result.branch}/{result.status}] {result.message}")
    for source in result.evidence:
        location = f" · {source['file']}" if "file" in source else ""
        print(f"来源：{source['title']} v{source['version']} / {source['section']}（{source['source_id']}）{location}")
    if result.missing_slots:
        print("缺少：" + "、".join(result.missing_slots))
    if result.data:
        print(json.dumps(result.data, ensure_ascii=False, indent=2))
    if result.pending_action:
        print(f"确认命令：/确认 {result.pending_action.operation_id}；或 /取消")
    if result.receipt:
        print(f"回执：{result.receipt.receipt_id}")


def run_demo(agent):
    def turn(text, expected):
        print(f"> {text}")
        result = agent.handle(replace(DEMO_IDENTITY, request_id=str(uuid4())), text)
        render(result)
        if result.status != expected:
            raise RuntimeError(f"演示未达到预期状态 {expected}，实际为 {result.status}")
        return result

    turn("退货超期能不能受理？", "answered")
    turn("查询订单 ORD-1001", "answered")
    draft = turn("创建售后工单 ORD-1001，原因：超过示例退货期限，申请人工核对", "needs_confirmation")
    written = turn(f"/确认 {draft.pending_action.operation_id}", "succeeded")
    ticket_id = written.data["ticket"]["ticket_id"]
    looked_up = turn(f"查询工单 {ticket_id} 状态", "answered")
    if looked_up.data["ticket"]["status"] != "waiting_for_agent":
        raise RuntimeError("演示工单未处于待人工受理状态")


def main(argv=None):
    from argparse import ArgumentParser

    parser = ArgumentParser(description="电商商家客服：离线规则 + 本地合成数据")
    parser.add_argument("--demo", action="store_true", help="运行固定售后场景")
    parser.add_argument("--rag", action="store_true", help="启用本地政策检索")
    parser.add_argument("--rebuild-index", action="store_true", help="启用 RAG 时构建政策索引")
    parser.add_argument("--rag-root", type=Path, default=Path("runtime/rag"))
    parser.add_argument("--policy-catalog", type=Path, default=Path("data/merchant_policies/catalog.json"))
    args = parser.parse_args(argv)
    if args.rebuild_index and not args.rag:
        parser.error("--rebuild-index 需要 --rag")
    with ExitStack() as stack:
        directory = stack.enter_context(TemporaryDirectory(prefix="merchant-support-"))
        database = Database(Path(directory) / "demo.sqlite3")
        database.initialize()
        seed_demo(database)
        from saas_support_agent.conversation.model import configured_model
        from saas_support_agent.conversation.understanding import ModelUnderstanding
        from saas_support_agent.knowledge.generation import EvidenceGenerator

        model = configured_model()
        if model is not None:
            stack.callback(model.close)
        agent = MerchantSupport(database, understanding=ModelUnderstanding(model) if model else None)
        if args.rag:
            from saas_support_agent.knowledge.models import LocalModels
            from saas_support_agent.knowledge.retrieval import PolicyIndex, PolicyKnowledge, build_index

            try:
                models = LocalModels(args.rag_root / "models")
                index_root = args.rag_root / "index"
                if args.rebuild_index:
                    build_index(index_root, args.policy_catalog, models)
                index = stack.enter_context(closing(PolicyIndex(index_root, args.policy_catalog, models)))
            except Exception as error:
                parser.exit(2, f"政策检索暂不可用（{type(error).__name__}）：请检查本地模型和索引。\n")
            agent.knowledge = PolicyKnowledge(index, agent.notifications.service,
                                              generator=EvidenceGenerator(model) if model else None)
        print(("可选模型模式" if model else "离线规则模式") + "：使用合成数据，退出后清理临时数据库。")
        if args.demo:
            run_demo(agent)
            return
        print("可输入：退货超期能不能受理？／查询订单 ORD-1001／创建售后工单 ORD-1001")
        print("补参示例：/补充 原因：需要人工核对；/恢复 显示当前草稿；/退出 结束。")
        while True:
            try:
                text = input("> ")
            except (EOFError, KeyboardInterrupt):
                return
            if text.strip() == "/退出":
                return
            render(agent.handle(replace(DEMO_IDENTITY, request_id=str(uuid4())), text))


if __name__ == "__main__":
    main()
