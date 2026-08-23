import os

def append(source, dest):
    for subdir, _, files in os.walk(source):
        for file in files:
            target_path = os.path.join(dest, file)
            print(target_path)
            if not os.path.exists(target_path):
                raise ValueError("FileNotFound")

            source_path = os.path.join(subdir, file)
            with open(source_path, 'r', encoding="utf-8") as source_file:
                new_lines = source_file.readlines()

            with open(target_path, "r+", encoding='utf-8') as f:
                old_lines = f.readlines()
                old_content = ''.join(old_lines)

                first_resource = next(
                    (l.strip() for l in new_lines if l.strip() and not l.strip().startswith('<?')),
                    None
                )
                if first_resource and first_resource in old_content:
                    continue

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
