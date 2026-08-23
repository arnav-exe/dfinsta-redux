param(
    [Parameter(Mandatory=$true)]
    [string]$ApkPath
)

apktool d $ApkPath -o instagram_source
