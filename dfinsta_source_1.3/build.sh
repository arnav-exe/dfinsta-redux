set -e
source ~/.zshrc
python remove_duplicate_style_tag.py
python append_public.py
python append_res.py
python append_to_manifest.py
cp -a newCode/. instagram_source/smali_classes7
cp -a overwriteCode/. instagram_source/
cp -a newRes/. instagram_source/res
rm -f instagram_source/assets/drawables.bin
rm -f dfinsta.unaligned.apk
rm -f "dfinsta_$1.apk"
apktool build instagram_source -o dfinsta.unaligned.apk
#jarsigner -verbose -sigalg MD5withRSA -digestalg SHA1 -keystore ~/.android/debug.keystore -storepass android dfinsta.unaligned.apk androiddebugkey
zipalign -v 4 dfinsta.unaligned.apk "dfinsta_$1.apk"
apksigner sign --ks ~/.android/dfinsta-release-key.keystore "dfinsta_$1.apk"
adb install "dfinsta_$1.apk"
