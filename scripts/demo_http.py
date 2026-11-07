"""对已启动的本机服务运行合成售后演示；不打印身份令牌。"""
import json
import os
from urllib.request import Request, urlopen


def main():
    token = os.environ['SAAS_DEMO_CLIENT_TOKEN']
    base = 'http://127.0.0.1:8000'

    def post(path, payload):
        request = Request(base + path, data=json.dumps(payload).encode(),
                          headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    session = post('/v1/sessions', {'workspace_id': 'demo-workspace'})
    root = '/v1/sessions/' + session['session_id']

    def message(text):
        return post(root + '/messages', {'text': text})

    policy = message('退货超期能不能受理？')
    assert policy['status'] == 'answered' and policy['evidence']
    print('政策引用：', policy['evidence'][0]['source_id'])
    assert message('查询订单 ORD-1001')['data']['order']['status'] == 'delivered'
    task = message('创建售后工单')['task']['task_id']
    # 此演示用文本补参；结构化 PATCH 接口另有自动化测试。
    prepared = message('补充 ORD-1001，原因：超期需要人工核对')
    assert prepared['task']['task_id'] == task
    binding = prepared['draft']['confirmation']
    result = post(root + '/task/confirm', binding)
    assert result['status'] == 'succeeded'
    repeat = post(root + '/task/confirm', binding)
    assert repeat['receipt']['receipt_id'] == result['receipt']['receipt_id']
    ticket = result['data']['ticket']['ticket_id']
    assert message(f'查询工单 {ticket}')['data']['ticket']['status'] == 'waiting_for_agent'
    print('工单：', ticket, '回执：', result['receipt']['receipt_id'])
    print('HTTP 售后闭环通过；重复确认返回相同回执。仅使用本地合成数据。')


if __name__ == '__main__':
    main()
