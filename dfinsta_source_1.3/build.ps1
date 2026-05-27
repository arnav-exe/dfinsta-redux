param(
    [Parameter(Mandatory=$true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "RUNNING remove_duplicate_style_tag.py" -ForegroundColor cyan
python remove_duplicate_style_tag.py

Write-Host ""
Write-Host "RUNNING append_public.py" -ForegroundColor cyan
python append_public.py

Write-Host ""
Write-Host "RUNNING append_res.py" -ForegroundColor cyan
python append_res.py

Write-Host ""
Write-Host "RUNNING append_to_manifest.py" -ForegroundColor cyan
python append_to_manifest.py

Write-Host ""
Write-Host "DOING SOME Copy-Item COMMANDS" -ForegroundColor cyan
Copy-Item -Path "newCode\*" -Destination "instagram_source\smali_classes7" -Recurse -Force
Copy-Item -Path "overwriteCode\*" -Destination "instagram_source" -Recurse -Force
Copy-Item -Path "newRes\*" -Destination "instagram_source\res" -Recurse -Force

Write-Host ""
Write-Host "REMOVING SOME TEMP DIRECTORIES" -ForegroundColor cyan
Remove-Item -Path "instagram_source\assets\drawables.bin" -Force -ErrorAction SilentlyContinue
Remove-Item -Path "dfinsta.unaligned.apk" -Force -ErrorAction SilentlyContinue
Remove-Item -Path "dfinsta_$Version.apk" -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "RUNNING apktool build" -ForegroundColor cyan
apktool build instagram_source -o dfinsta.unaligned.apk --use-aapt1

Write-Host ""
Write-Host "RUNNING zipalign" -ForegroundColor cyan
zipalign -v 4 dfinsta.unaligned.apk "dfinsta_$Version.apk"

Write-Host ""
Write-Host "SETTING keystorePath" -ForegroundColor cyan
$keystorePath = "$env:USERPROFILE\.android\dfinsta-release-key.keystore"

Write-Host ""
Write-Host "RUNNING apksigner" -ForegroundColor cyan
apksigner sign --ks $keystorePath "dfinsta_$Version.apk"

Write-Host ""
Write-Host "RUNNING adb install" -ForegroundColor cyan
adb install "dfinsta_$Version.apk"
