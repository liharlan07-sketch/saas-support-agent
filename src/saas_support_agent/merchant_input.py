"""离线意图与参数识别。仅匹配声明的句式，不调用语言模型。"""

from dataclasses import dataclass, field
import re


ORDER = r"ORD-[A-Z0-9]{1,24}"
SHOP = r"SHOP-[A-Z0-9]{1,24}"
TICKET = r"T-[A-F0-9]{16}"
ORDER_TARGET = rf"(?:(?P<shop_id>{SHOP})\s*[/／ ]\s*)?(?P<order_id>{ORDER})"
DESCRIPTION = r"(?:\s*[,，;；]?\s*(?:原因|描述)\s*[:：]\s*(?P<description>.+))?"
END = r"[。？?]?"


@dataclass(frozen=True)
class ParsedRequest:
    intent: str
    slots: dict = field(default_factory=dict)


def _slots(match):
    return {key: (value.upper() if key in {"shop_id", "order_id"} else value.strip())
            for key, value in match.groupdict().items() if value is not None}


def recognize(text: str) -> ParsedRequest:
    text = text.strip()
    match = re.fullmatch(rf"(?:查询|查)\s*(?:订单\s*)?{ORDER_TARGET}(?:\s*(?:的)?状态)?{END}", text, re.I)
    if match:
        return ParsedRequest("order_query", _slots(match))
    if re.fullmatch(r"(?:查询|查)\s*订单[。？?]?", text):
        return ParsedRequest("order_query")
    match = re.fullmatch(rf"(?:查询|查)\s*(?:工单\s*)?(?P<ticket_id>{TICKET})(?:\s*状态)?{END}", text, re.I)
    if match:
        return ParsedRequest("ticket_query", {"ticket_id": "T-" + match["ticket_id"][2:].lower()})
    if re.fullmatch(r"(?:查询|查)\s*(?:售后)?工单(?:状态)?[。？?]?", text):
        return ParsedRequest("ticket_query")
    match = re.fullmatch(
        rf"(?:创建|提交|申请)(?:一张|一个)?售后工单(?:\s*(?:订单\s*)?{ORDER_TARGET})?{DESCRIPTION}", text, re.I,
    )
    if match:
        return ParsedRequest("ticket_start", _slots(match))
    match = re.fullmatch(rf"(?:/补充|补充)\s*(?:(?:订单\s*)?{ORDER_TARGET})?{DESCRIPTION}", text, re.I)
    if match and match.group(0).strip() not in {"/补充", "补充"}:
        return ParsedRequest("ticket_edit", _slots(match))
    match = re.fullmatch(r"(开启|关闭|查询)(任务指派|评论提及)(邮件|站内)通知[。？?]?", text)
    if match:
        verb, kind, channel = match.groups()
        slots = {
            "notification_type": {"任务指派": "task_assigned", "评论提及": "comment_mentioned"}[kind],
            "channel": {"邮件": "email", "站内": "in_app"}[channel],
        }
        if verb == "查询":
            return ParsedRequest("notification_query", slots)
        return ParsedRequest("notification_update", {**slots, "enabled": verb == "开启"})
    if re.fullmatch(r"(?:咨询\s*)?(?:退货超期政策|退货超期能不能受理|退货超期怎么处理)[。？?]?", text):
        return ParsedRequest("policy_query", {"topic": "late_return"})
    if re.fullmatch(r"(?:咨询\s*)?(?:退货期限|退货时限|多久可以申请退货)[。？?]?", text):
        return ParsedRequest("policy_query", {"topic": "return_window"})
    if text.startswith("咨询") or text.endswith(("？", "?")):
        return ParsedRequest("policy_query")
    return ParsedRequest("unsupported")
