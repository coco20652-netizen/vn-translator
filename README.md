# 视觉小说翻译器

给自己做的视觉小说翻译工具。填一个 DeepSeek 的 API key 就能用，选好游戏点一下，翻完直接进游戏玩中文版。

- Ren'Py 视觉小说：整本批量翻译，装成补丁。补丁不改游戏原文件，删掉就恢复原样。
- Unity 游戏：边玩边翻，翻过的句子存下来，下次不再花钱。
- 默认用 DeepSeek，也能填其他兼容 OpenAI 接口的服务；会显示 tokens 用量和花了多少钱。

## 下载使用（Windows）

1. 到 [Releases](../../releases/latest) 下载 `VNTranslator-windows-x64.zip`
2. 解压，双击「启动.bat」
3. 「设置」里填 DeepSeek API key，选游戏文件夹，勾上游戏点「翻译勾选的游戏」

自带 Python 运行时，不用另外装。

## 说明

- API key 只存在本机工具文件夹的 `config.json` 里，不会上传。
- 翻译记录在 `data/`，用量在 `用量记录.csv`。
- 个人自用工具，游戏文本版权归原作者。
