import os

def append(source, dest):
    for subdir, dirs, files in os.walk(source):
        for file in files:
            target_path = os.path.join(dest, file)
            print(target_path)
            if not os.path.exists(target_path):
                raise ValueError("FileNotFound")

            source_path = os.path.join(subdir, file)
            with open(source_path, 'r') as source_file:
                new_lines = source_file.readlines()

            with open(target_path, "r+") as f:
                old_lines = f.readlines()
                del old_lines[-1]
                old_lines += new_lines
                old_lines.append("</resources>\n")

                f.seek(0)
                f.writelines(old_lines)
                f.truncate()

source = "appendRes/values"
dest = "instagram_source/res/values"

append(source, dest)

source = "appendRes/values-night"
dest = "instagram_source/res/values-night"

append(source, dest)