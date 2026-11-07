"""同一确认状态机用于工单，确认后原样提交已经展示的内容。"""

from saas_support_agent.core.models import SupportTicketRequest, TICKET_ACTION_TYPE
from saas_support_agent.services.support_ticket import validate_ticket_patch
from saas_support_agent.workflows.notification_update import NotificationUpdateWorkflow
from saas_support_agent.workflows.task_context import TaskContextManager


class SupportTicketWorkflow(NotificationUpdateWorkflow):
    def _summary(self, identity, request, current):
        target = (f"店铺：{request.shop_id}；订单：{request.order_id}" if request.order_id
                  else f"关联项目：{request.project_id or '无'}")
        return (
            f"工作空间 {identity.workspace_id}，以本人 {identity.user_id} 创建待人工受理工单。"
            f"\n问题类型：{request.issue_type}\n问题描述：{request.description}"
            f"\n{target}\n复现步骤：{request.steps or '无'}"
            f"\n观察到的错误：{request.observed_error or '无'}"
        )


class SupportTicketTasks(TaskContextManager):
    action_type = TICKET_ACTION_TYPE
    request_type = SupportTicketRequest
    def __init__(self, workflow: SupportTicketWorkflow):
        super().__init__(workflow)
        self.required_slots = frozenset(self.service.required_slots)

    _validate_patch = staticmethod(validate_ticket_patch)
