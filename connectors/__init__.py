"""
connectors 包 —— 连接器框架（2026-10-25 执行层建设第二批）。

定位：Grok Bot 能"登录你已有的 App 干活"。我们做**框架 + 两个真连接器**：
GitHub（token 即用）与 IMAP 邮件（应用密码即可，无需 OAuth 跳转）。
需要 OAuth 授权流程的连接器（Google/Slack 官方）只留接口，等用户本人
授权联调——不代做、不静默跳过。

纪律：
- 纯标准库（urllib / imaplib），零第三方依赖；
- 凭据只从环境变量/config 读，**绝不硬编码、绝不落库、绝不打印**；
- 未配置凭据时 available()=False，call() 明确报错，不静默造假；
- 测试全部走注入的假传输层（FakeTransport/FakeIMAP），不碰网络。
"""

from .base import Connector, ConnectorError, ConnectorResult               # noqa: F401
from .github import GitHubConnector                                       # noqa: F401
from .imap import IMAPConnector, EmailRule, EmailTrigger                   # noqa: F401
