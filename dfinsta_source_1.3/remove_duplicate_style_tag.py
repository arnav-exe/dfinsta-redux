styles = "instagram_source/res/values/styles.xml"

offending = '<item name="android:textColorHint">?textColorTertiary</item>'
lines = []
with open(styles, "r+") as f:
    for line in f.readlines():
        if not offending in line:
            lines.append(line)
        else:
            print("Successfully removed duplicated line")

    f.seek(0)
    f.writelines(lines)
    f.truncate()
