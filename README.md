# DM4 4D-STEM Align Export GUI

A desktop GUI tool for loading 4D-STEM DM4 files, performing optional frame alignment, applying navigation and diffraction binning, and exporting the processed result as MRC or raw IMG stack data.

This project is designed for users who want a simple Windows-based interface instead of writing custom preprocessing scripts. It supports both ROI-based center-spot alignment and external alignment loaded from `.mat` files, making it suitable for interactive inspection as well as batch-style preprocessing workflows.

## Features

- Load 4D-STEM data from `.dm4` files
- Detect and process 4D signal data through a GUI
- Optional alignment before binning and export
- ROI-based center-spot alignment using an interactively selected reference region
- Alignment import from `alignment.mat`
- Navigation binning and diffraction binning
- Export processed stack to:
  - `MRC`
  - raw `IMG` float32 stack
- Save auxiliary outputs such as:
  - shift table CSV
  - shift curve CSV
  - shift plot PNG
  - centers/shifts NPY
  - format description JSON/TXT

## Typical Workflow

1. Launch the GUI
2. Select a `.dm4` input file
3. Choose an output directory
4. Inspect input shape
5. Configure binning parameters
6. Enable or disable alignment
7. If using ROI alignment, select a reference ROI interactively
8. Start processing
9. Export the aligned and binned stack

## Output

The program can generate:

- `*.mrc` for MRC-compatible workflows
- `*.img` as raw float32 stack output
- `*_format_info.json` and `*_format_info.txt`
- `*_shifts.npy`
- `*_centers.npy`
- `*_shifts.csv`
- `*_shift_curve.csv`
- `*_shift_plot.png`

## Packaging

The project includes a Windows packaging script based on PyInstaller:

- `build_exe.bat`

The generated release package can be distributed as a standalone Windows GUI application.

## Requirements

Main Python dependencies:

- `numpy`
- `matplotlib`
- `hyperspy`
- `mrcfile`
- `scipy`
- `pyinstaller`

See `requirements.txt` for details.

## Notes

- Alignment is applied before binning
- If the input dimensions are not divisible by the selected binning factors, the data will be trimmed
- Raw `IMG` output is headerless float32 data, so the accompanying format info file should be kept together with the exported stack
