import os


import os

folder = r"C:\Users\Jacobs laptop\Downloads\Telegram Desktop\ChatExport_2026-04-23 (2)\photos"

for filename in os.listdir(folder):
    if "_thumb" in filename:
        print(filename)

folderw = r"C:\Users\Jacobs laptop\Downloads\Telegram Desktop\ChatExport_2026-04-23 (2)"

for filename in os.listdir(folder):
    if filename.lower().endswith("_thumb.jpg"):
        file_path = os.path.join(folder, filename)
        os.remove(file_path)
        print(f"Deleted: {filename}")

print("Done.")