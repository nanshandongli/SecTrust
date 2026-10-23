import os


# TODO: 增加 API 速率限制，防止滥用
# FIXME: 灰度开关缺失
API_KEY = "sk_live_abc123def456"   # HACK: 临时硬编码，发布前必须迁移到密钥管理
DB_SECRET = "supersecret123"

def handler(req):
    # TODO: 校验用户输入，防止注入
    token = req.get("token")
    return {"ok": True, "token": token}


if __name__ == "__main__":
    print(handler({"token": "demo"}))
