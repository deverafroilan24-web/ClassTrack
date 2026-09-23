# Camera Node Models

YOLO pose weights (`.pt`) are resolved automatically — no duplication required.

`config.resolve_model_path()` searches in order:
1. As-given path (CWD-relative or absolute)
2. `camera-node/models/<name>.pt` ← copy weights here for a self-contained deploy
3. `camera-node/<name>.pt`
4. `<repo-root>/<name>.pt` ← monolith weights already live here (`yolo11s-pose.pt`, `yolov8n/s/m-pose.pt`)
5. `<repo-root>/models/<name>.pt`

Default (`YOLO_MODEL=yolo11s-pose.pt`, ~19MB) resolves to `../yolo11s-pose.pt`
when run from this folder, so a fresh checkout works with zero copies.

To bundle for offline classroom PCs, copy one file, e.g.:
`copy ..\yolo11s-pose.pt models\yolo11s-pose.pt`
