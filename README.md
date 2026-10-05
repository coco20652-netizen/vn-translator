# 游戏汉化工具

用你自己的大模型 API key，把下载的 Ren'Py / Unity 游戏翻成简体中文。

- Ren'Py：读取游戏脚本（.rpyc / .rpa），批量翻译后装一个中文补丁。补丁不改游戏原文件，只新增 `game/zz_cn_patch.rpy` 和 `game/zz_cn/`，删掉就恢复原样。
- Unity：自动给游戏装 BepInEx + XUnity.AutoTranslator，边玩边翻，翻过的句子会存下来，下次不再花钱。
- 支持 DeepSeek，以及其他兼容 OpenAI 接口的服务；显示 tokens 用量和费用估算。

## 下载使用（Windows）

1. 到 [Releases](../../releases/latest) 下载 `GameCNTool-windows-x64.zip`
2. 解压到任意位置，双击「启动.bat」
3. 在「设置」里填 API key，选好游戏文件夹，勾上游戏点「翻译勾选的游戏」

压缩包自带 Python 运行时，不用另外安装 Python。

## 说明

- API key 只保存在本机工具文件夹的 `config.json` 里，不会上传。
- 翻译记录在 `data/` 文件夹，用量在 `用量记录.csv`。
- 游戏文本版权归原作者，本工具仅供个人学习使用。

## 自己打包

推送 `v*` 标签时 GitHub Actions 会在 Windows 上运行 `build_windows.py`，生成带运行时的 zip 并发布到 Releases。
