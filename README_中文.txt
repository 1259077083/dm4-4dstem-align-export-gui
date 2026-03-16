DM4 4D-STEM 对齐与导出工具
=========================

文件说明
--------
1. dm4_align_export_gui.py
   主程序。用 Tkinter 做 GUI，可选择 DM4 文件、修改参数、框选 ROI、执行 alignment、输出 MRC/IMG。

2. run_dm4_gui.bat
   点击运行版本。Windows 下可直接双击启动；优先尝试用 conda 的 abtem 环境运行。

3. build_exe.bat
   在 Windows 上把本工具打包为 exe 的示例脚本。采用 PyInstaller 的 onedir 模式，通常比 onefile 更稳。

4. requirements.txt
   依赖列表。

主要功能
--------
- 选择 DM4 文件
- 选择输出目录
- 修改 navigation bin / diffraction bin
- 可选 alignment
- 可在参考衍射图上交互框选 ROI，用于中心斑搜索
- 可直接导入 alignment.mat，使用外部位移结果做对齐
- 可输出 mrc 或 img
- 自动输出格式说明文件（JSON 和 TXT）
- 自动输出 shift 表格 / shift 曲线 CSV / shift 折线图 PNG

输出文件说明
------------
1. *.mrc
   输出为 float32 MRC 堆栈，shape = (n_frames, out_kx, out_ky)

2. *.img
   输出为 headerless raw float32 堆栈。实际 shape、dtype、frame 顺序请查看 companion 的 format_info.json / format_info.txt。

3. *_format_info.json / *_format_info.txt
   记录输入形状、裁剪形状、输出形状、binning、ROI、reference nav、reference center、frame order 等。

4. *_centers.npy / *_shifts.npy
   记录 alignment 时每个原始 frame 的中心坐标和位移。

5. *_shifts.csv
   记录 nav_y, nav_x, center_y, center_x, shift_y, shift_x

6. *_shift_curve.csv
   记录 frame_index, shift_y, shift_x

7. *_shift_plot.png
   x 轴为 frame index，y 轴为 shift_y / shift_x

使用步骤
--------
1. 双击 run_dm4_gui.bat
2. 选择 DM4 文件
3. 选择输出目录
4. 先点“读取 shape”确认数据尺寸
5. 如需 alignment：
   - 勾选“启用 alignment”
   - 可选模式 A：ROI 搜索中心斑
     * 点“选择 ROI”
     * 在弹出的参考衍射图上拖框，按 Enter 确认
   - 可选模式 B：导入 alignment.mat
     * 选择 alignment.mat 文件
     * 默认变量名为 alignment
     * 选择 MAT 两列的含义（dx_dy 或 dy_dx）
6. 选择输出格式（mrc / img）
7. 设置 navigation bin 和 diffraction bin
8. 点“开始处理”

注意事项
--------
1. alignment 是先逐帧对齐，再做 binning。
2. 如果数据尺寸不能被 bin 整除，程序会自动裁剪尾部数据，并在日志及 format_info 中写明。
3. img 不是通用标准容器，这里采用的是“无头原始 float32 堆栈”，请一定结合 format_info 文件使用。
4. 若要把本工具打包成 exe，建议在 Windows 的目标环境中执行 build_exe.bat。
