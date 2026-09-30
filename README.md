# 航拍道路拼接工具

这是一个面向 Windows 的离线桌面工具，用于把垂直向下拍摄、保持相对稳定高度并平移飞行的航拍视频拼接成完整道路或地面影像。

当前版本的核心能力：

- 自动读取视频分辨率、帧率和时长；
- 根据代表性画面的重叠率推荐抽帧间隔；
- “沿道路拼接”模式以道路标线、护栏和道路边缘为主要匹配特征；
- “自由平移拼接”模式使用整幅地面特征，支持单方向及弯曲路线；
- 通过相邻帧稳健匹配累积全局位置和航向，保留真实转弯轨迹；
- 自由模式按原始相机轨迹直接展开，不自动旋转或压缩整条路线；
- 自由模式在重叠区域自动寻找低差异接缝，并保留单一来源像素，避免标线和护栏重影；
- 支持直接拖入视频，并显示分辨率、时长、大小和帧率摘要；
- 两种拼接模式采用直接切换，结果目录会标明自动或自定义位置；
- 生成预览图、高清图和接缝抽查图；
- 保存任务状态，已完成阶段可以续算；
- 支持暂停、继续和取消。

界面中的模式选择：

- **沿道路拼接**：适合道路始终位于画面中央附近的传统直线或缓弯任务；
- **自由平移拼接**：适合画面沿任意单向路线平移，途中可以转弯，结果会按实际飞行轨迹展开。

## 开发运行

推荐使用 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_road_stitcher.py
```

命令行处理：

```powershell
.\.venv\Scripts\python.exe run_road_stitcher.py run "D:\video.mp4" "D:\result"
```

自由平移模式：

```powershell
.\.venv\Scripts\python.exe run_road_stitcher.py run "D:\video.mp4" "D:\result" --mode free
```

运行自动化测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

手动指定抽帧间隔：

```powershell
.\.venv\Scripts\python.exe run_road_stitcher.py run "D:\video.mp4" "D:\result" --mode free --interval 2
```

## Windows 构建

```powershell
.\build_windows.ps1
```

构建结果位于：

```text
dist\RoadStitcher\
dist\RoadStitcher-0.3.8-windows-x64.zip
```

当前采用 PyInstaller 文件夹模式，优先保证视频组件和 Qt 插件加载稳定。将整个文件夹或 ZIP 解压后的整个文件夹一起复制，不要只复制 EXE。

## 公开仓库说明

本仓库不包含实拍视频、抽帧缓存、拼接结果、构建产物或本地开发环境。请使用你有权处理的航拍素材进行测试。

## 工程结构

```text
road_stitcher/
  config.py          任务配置
  state.py           状态、暂停、取消和断点文件
  media.py           视频检查及中文路径图像读写
  interval.py        自动抽帧间隔推荐
  extractor.py       可续算抽帧
  legacy_bridge.py   调用经过实拍验证的拼接核心
  free_stitch.py     自由平移配准、全局轨迹和弯曲画布融合
  pipeline.py        完整任务编排
  ui.py              中文 Windows 界面
  cli.py             命令行入口
```

根目录中的以下脚本是经过当前实拍视频验证的算法基线：

- `analyze_aerial_overlap.py`
- `optimize_aerial_road_stitch.py`

在新引擎完全通过回归测试前，不应删除这些算法基线。

## 输出文件

每个任务的结果目录根部只放最终拼接图片，并包含一个
`处理数据` 子目录。抽帧、日志、缓存和诊断报告统一保存在
`处理数据` 中：

- `处理数据/job.json`：任务状态和结果清单；
- `处理数据/job.log`：处理日志；
- `处理数据/video_info.json`：视频参数；
- `处理数据/interval_report.json`：自动抽帧评估结果；
- `处理数据/frames_*s`：拼接抽帧；
- 沿道路模式的预览图和高清图位于结果根目录，精确对齐缓存和接缝检查图位于 `处理数据`；
- 自由平移模式的预览图和高清图位于结果根目录，配准缓存和报告位于 `处理数据`。

## 已知边界

- 自由平移模式要求镜头基本垂直向下、飞行高度相对稳定，并且相邻抽帧之间保留足够的地面重叠；
- 当前支持连续的单向或弯曲路线，尚未实现往返扫描、闭环校正和测绘级网格航线优化；
- 沿道路模式已对源帧边缘做渐隐和有效区约束，避免透视变换边界形成横向白色接缝；
- 视频拼接不能替代带有相机姿态、GPS和地面控制点的测绘正射影像；
- 车辆、树冠和高层建筑可能因运动或视差产生局部重影；
- 暂停在抽帧阶段可以即时生效，在特征计算和渲染阶段会在安全阶段边界生效；
- 高清成果超过普通 PSD 尺寸限制时，应在 Photoshop 中保存为 PSB。
