param(
    [Parameter(Mandatory=$true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"

python remove_duplicate_style_tag.py
python append_public.py
python append_res.py
python append_to_manifest.py

Copy-Item -Path "newCode\*" -Destination "instagram_source\smali_classes7" -Recurse -Force
Copy-Item -Path "overwriteCode\*" -Destination "instagram_source" -Recurse -Force
Copy-Item -Path "newRes\*" -Destination "instagram_source\res" -Recurse -Force

Remove-Item -Path "instagram_source\assets\drawables.bin" -Force -ErrorAction SilentlyContinue
Remove-Item -Path "dfinsta.unaligned.apk" -Force -ErrorAction SilentlyContinue
Remove-Item -Path "dfinsta_$Version.apk" -Force -ErrorAction SilentlyContinue

apktool build instagram_source -o dfinsta.unaligned.apk

zipalign -v 4 dfinsta.unaligned.apk "dfinsta_$Version.apk"

$keystorePath = "$env:USERPROFILE\.android\dfinsta-release-key.keystore"
apksigner sign --ks $keystorePath "dfinsta_$Version.apk"

adb install "dfinsta_$Version.apk"
