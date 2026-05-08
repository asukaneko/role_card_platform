# 角色卡平台

一个独立运行的角色卡上传与分享平台。

## 启动

```powershell
cd role_card_platform
python server.py
```

可以复制 `.env.example` 为 `.env` 后填写服务端配置：

```env
ROLE_CARD_PORT=7861
ROLE_CARD_SECRET_KEY=一段随机字符串
ROLE_CARD_ADMIN_TOKEN=管理后台令牌
```

默认访问地址：

```text
http://127.0.0.1:7861
```

可用环境变量：

```text
ROLE_CARD_PORT=7861
ROLE_CARD_SECRET_KEY=任意随机字符串
ROLE_CARD_ADMIN_TOKEN=管理令牌
```

如果没有设置 `ROLE_CARD_ADMIN_TOKEN`，首次启动会自动生成并保存到：

```text
role_card_platform/data/admin_token.txt
```

管理页：

```text
http://127.0.0.1:7861/admin?token=你的令牌
```

## 功能

- 角色卡广场
- 角色详情页
- JSON 上传
- NekoBot 导出的 ZIP 上传（单张 `character.json + portrait.*`，以及批量导出的多文件夹 ZIP）
- NekoBot “我的角色卡”可通过接口上传到平台
- 表单创建角色卡
- 头像上传
- 标签筛选
- 下载角色卡 JSON
- 下载 NekoBot 可导入的 ZIP
- 喜欢计数
- 管理员隐藏、公开、删除

## 和 NekoBot 连接

NekoBot 默认会把角色上传到：

```text
http://127.0.0.1:7861
```

如果平台部署在别的服务器，在启动 NekoBot 前设置：

```text
ROLE_CARD_PLATFORM_URL=http://你的服务器:7861
```

在平台注册账号，获取 API 令牌，然后在 NekoBot 侧设置：

```text
ROLE_CARD_PLATFORM_TOKEN=你的令牌
```

## 角色卡 JSON 字段

支持常见字段：

```json
{
  "name": "角色名",
  "description": "简介",
  "personality": "性格设定",
  "scenario": "场景",
  "first_message": "开场白",
  "system_prompt": "系统提示词",
  "tags": ["标签"],
  "creator": "作者"
}
```
