# Safe Space Maker

A Python/Tkinter GUI that scans selected drives and shows the largest space-consuming files and cleanup candidates.

It is powerful but still guarded:

- Shows drive usage and highlights the drive using the most space.
- Deep audit mode shows all large accessible files, including hidden files.
- Protected Windows/app paths are shown for audit but blocked from deletion.
- Cleanup scan mode suggests likely non-essential candidates such as temp folders, cache folders, crash dumps, logs, backups, and large reviewable files in Downloads.
- `Select all`, `Select deletable`, and `Clear selection` help with bulk cleanup.
- The **Never delete paths** box lets you type or add selected files/folders that must never be deleted.
- Never deletes automatically. You select items and confirm one batch delete.
- On Windows, selected items are moved to the Recycle Bin when possible.

## Run

```powershell
python space_maker.py
```

If `python` is not on PATH, try:

```powershell
py space_maker.py
```

## Safety Notes

No cleanup tool can perfectly know what is personally important to you. This app avoids essential system locations and only lists conservative cleanup candidates, but you should still review selected files before confirming deletion.

The app blocks deletion for:

- Windows and boot folders
- Program Files folders
- ProgramData
- Root drives
- Application data that is not a recognized cleanup/temp/cache candidate
- Links and Windows reparse points/junctions
- Anything you add to **Never delete paths**

## Recommended Use

1. Launch the app.
2. Keep the default minimum size, or raise it for faster scanning.
3. Keep **Deep audit** enabled to see all large accessible files, including hidden files.
4. Scan selected drives.
5. Review the biggest files first.
6. Use **Select all** or **Select deletable** if you want bulk selection.
7. Add anything important to **Never delete paths**.
8. Click **Delete selected...** and confirm.

## Recycle Bin Failures

If Windows returns a recycle error such as code `124`, the app now retries each selected item one by one. Files that can be recycled are removed from the table, and locked/missing/too-long/non-recyclable paths are reported without stopping the whole cleanup.
