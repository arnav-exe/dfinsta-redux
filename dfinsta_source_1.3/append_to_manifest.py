manifest = "instagram_source/AndroidManifest.xml"

new_activities= [
'       <activity android:name="com.dfinstagram.IconChoose" android:theme="@style/DfInstagramPreference"/>\n'
'       <activity android:name="com.dfinstagram.preference.Preference" android:screenOrientation="portrait" android:theme="@style/DfInstagramPreference"/>\n'
]

lines = []
added = False
with open(manifest, "r+") as f:
    for line in f.readlines():
        lines.append(line)
        if "</activity>" in line and not added:
            lines += new_activities
            print("appended new activities to manifest")
            added = True

    f.seek(0)
    f.writelines(lines)
    f.truncate()
