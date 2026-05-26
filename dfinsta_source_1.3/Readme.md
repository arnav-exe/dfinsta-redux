# Distraction Free Instagram

Ok so roughly how this thing works is that it uses apktool to decompile the instagram app, then we copy over
the new files and then recompile it. The tricky issue is that instagram obfuscates their code, meaning that the
class names get mangled and in particular they get mangled differently in every new version. This means that 
for every new version of instagram, you will need to edit all of the modified code so that it refers to the new class names.

To build and run this project you will need 
* [apktool](https://ibotpeaches.github.io/Apktool/)
* android SDK (including adb)
* python
* [jadx-gui](https://github.com/skylot/jadx). This is useful for reconstructing the .smali files and viewing them
as java classes

# Latest version

The lastest version is built using [this](https://www.apkmirror.com/apk/instagram/instagram-instagram/instagram-instagram-300-0-0-29-110-release/instagram-300-0-0-29-110-13-android-apk-download/) APK.
The previous version is built using [this](https://apkpure.com/instagram-android/com.instagram.android/download/366308885-APK) APK.

# Building

Firstly, extract the apk using `extract.sh Instagram_300.0.0.29.110.apk` or whatever apk you are using.

Then, run `build.sh`.

To rebuild, do `rm -rf instagram_source`

# Checklist for updating

* Modify all the files in overwriteCode so that they refer to the correct files which will have changed between 
versions
* modify newCode as well. There are classes in newCode which refer to classes within instagram and thus they will
need to be updated as well. Don't forget that the method names will have changed as well.
* Don't forget to update the ExternalSyntheticLambda0 in the PreferenceFragment so that it points to the correct
method on the FeedCacheCoordinator, which will have changes. It's the one that calls `invoke-virtual {v0}, Ljava/util/AbstractCollection;->clear()V`
* Update the version number in istrings
* Next time you update, check if JniHandler etc can be removed.

## Minor version releases
* Update the version number too!

# Behaviours to manually test
* Test that changing settings deletes the feed cache. 

# Tests
Tests are mostly copied from here
https://proandroiddev.com/the-definitive-guide-of-android-ui-automator-with-kotlin-2eab40edab0d

They are stored under ui-automator. At the moment there is only a single test which tests that the app
actually starts.

To run the tests, plug in your smartphone, install dfinstagram, then run `./ui-automator/run_test.sh`


# Helpful literature

[This](http://pallergabor.uw.hu/androidblog/dalvik_opcodes.html) explains the meaning of the various opcodes in the smali files