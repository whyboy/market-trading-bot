"""PushPlus 微信渠道，只记录受理结果，不将其视为送达回执。"""
import requests


class PushPlusClient:
    class Error(RuntimeError):
        def __init__(self, message, retryable=False):
            super().__init__(message)
            self.retryable = retryable

    def __init__(self, token):
        self.token = token

    def send(self, title, content):
        try:
            response = requests.post(
                'https://www.pushplus.plus/send',
                json={'token': self.token, 'title': title, 'content': content,
                      'template': 'txt', 'channel': 'wechat'}, timeout=(3, 10))
        except requests.RequestException:
            raise self.Error('PushPlus 网络请求失败', retryable=True) from None
        if response.status_code != 200:
            raise self.Error(f'PushPlus HTTP {response.status_code}',
                             retryable=response.status_code == 429 or response.status_code >= 500)
        try:
            payload = response.json()
            code = payload['code']
            receipt = payload.get('data')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise self.Error('PushPlus 响应格式错误', retryable=True) from None
        if code != 200:
            # 不记录响应正文，避免凭据或服务端回显内容进入日志。
            raise self.Error(f'PushPlus 拒绝请求（代码 {code}）', retryable=code == 500)
        return str(receipt) if receipt is not None else ''
