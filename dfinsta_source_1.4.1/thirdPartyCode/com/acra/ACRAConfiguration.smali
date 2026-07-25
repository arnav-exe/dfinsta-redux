.class public Lcom/acra/ACRAConfiguration;
.super Ljava/lang/Object;
.source "ACRAConfiguration.java"

# interfaces
.implements Lcom/acra/annotation/ReportsCrashes;


# instance fields
.field private mAdditionalDropboxTags:[Ljava/lang/String;

.field private mAdditionalSharedPreferences:[Ljava/lang/String;

.field private mApplicationLogFile:Ljava/lang/String;

.field private mApplicationLogFileLines:Ljava/lang/Integer;

.field private mConnectionTimeout:Ljava/lang/Integer;

.field private mCustomReportContent:[Lcom/acra/ReportField;

.field private mDeleteOldUnsentReportsOnApplicationStart:Ljava/lang/Boolean;

.field private mDeleteUnapprovedReportsOnApplicationStart:Ljava/lang/Boolean;

.field private mDisableSSLCertValidation:Ljava/lang/Boolean;

.field private mDropboxCollectionMinutes:Ljava/lang/Integer;

.field private mExcludeMatchingSettingsKeys:[Ljava/lang/String;

.field private mExcludeMatchingSharedPreferencesKeys:[Ljava/lang/String;

.field private mForceCloseDialogAfterToast:Ljava/lang/Boolean;

.field private mFormKey:Ljava/lang/String;

.field private mFormUri:Ljava/lang/String;

.field private mFormUriBasicAuthLogin:Ljava/lang/String;

.field private mFormUriBasicAuthPassword:Ljava/lang/String;

.field private mGoogleFormUrlFormat:Ljava/lang/String;

.field private mHttpHeaders:Ljava/util/Map;
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "Ljava/util/Map",
            "<",
            "Ljava/lang/String;",
            "Ljava/lang/String;",
            ">;"
        }
    .end annotation
.end field

.field private mHttpMethod:Lcom/acra/sender/HttpSender$Method;

.field private mIncludeDropboxSystemTags:Ljava/lang/Boolean;

.field private mLogcatArguments:[Ljava/lang/String;

.field private mLogcatFilterByPid:Ljava/lang/Boolean;

.field private mMailTo:Ljava/lang/String;

.field private mMaxNumberOfRequestRetries:Ljava/lang/Integer;

.field private mMode:Lcom/acra/ReportingInteractionMode;

.field private mReportType:Lcom/acra/sender/HttpSender$Type;

.field private mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

.field private mResDialogCommentPrompt:Ljava/lang/Integer;

.field private mResDialogEmailPrompt:Ljava/lang/Integer;

.field private mResDialogIcon:Ljava/lang/Integer;

.field private mResDialogOkToast:Ljava/lang/Integer;

.field private mResDialogText:Ljava/lang/Integer;

.field private mResDialogTitle:Ljava/lang/Integer;

.field private mResNotifIcon:Ljava/lang/Integer;

.field private mResNotifText:Ljava/lang/Integer;

.field private mResNotifTickerText:Ljava/lang/Integer;

.field private mResNotifTitle:Ljava/lang/Integer;

.field private mResToastText:Ljava/lang/Integer;

.field private mSendReportsInDevMode:Ljava/lang/Boolean;

.field private mSharedPreferenceMode:Ljava/lang/Integer;

.field private mSharedPreferenceName:Ljava/lang/String;

.field private mSocketTimeout:Ljava/lang/Integer;


# direct methods
.method public constructor <init>(Lcom/acra/annotation/ReportsCrashes;)V
    .locals 1

    const/4 v0, 0x0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mAdditionalDropboxTags:[Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mAdditionalSharedPreferences:[Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mConnectionTimeout:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mCustomReportContent:[Lcom/acra/ReportField;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteUnapprovedReportsOnApplicationStart:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteOldUnsentReportsOnApplicationStart:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mDropboxCollectionMinutes:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mForceCloseDialogAfterToast:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormKey:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUri:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthLogin:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthPassword:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mIncludeDropboxSystemTags:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mLogcatArguments:[Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mMailTo:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mMaxNumberOfRequestRetries:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mMode:Lcom/acra/ReportingInteractionMode;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogCommentPrompt:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogEmailPrompt:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogIcon:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogOkToast:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogText:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogTitle:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifIcon:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifText:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTickerText:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTitle:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResToastText:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceMode:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceName:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mSocketTimeout:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mLogcatFilterByPid:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mSendReportsInDevMode:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSharedPreferencesKeys:[Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSettingsKeys:[Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFile:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFileLines:Ljava/lang/Integer;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mGoogleFormUrlFormat:Ljava/lang/String;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mDisableSSLCertValidation:Ljava/lang/Boolean;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mHttpMethod:Lcom/acra/sender/HttpSender$Method;

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportType:Lcom/acra/sender/HttpSender$Type;

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    return-void
.end method

.method public static isNull(Ljava/lang/String;)Z
    .locals 1

    if-eqz p0, :cond_0

    const-string v0, "ACRA-NULL-STRING"

    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-eqz v0, :cond_1

    :cond_0
    const/4 v0, 0x1

    :goto_0
    return v0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method


# virtual methods
.method public additionalDropBoxTags()[Ljava/lang/String;
    .locals 2

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mAdditionalDropboxTags:[Ljava/lang/String;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mAdditionalDropboxTags:[Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->additionalDropBoxTags()[Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x0

    new-array v0, v1, [Ljava/lang/String;

    goto :goto_0
.end method

.method public additionalSharedPreferences()[Ljava/lang/String;
    .locals 2

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mAdditionalSharedPreferences:[Ljava/lang/String;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mAdditionalSharedPreferences:[Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->additionalSharedPreferences()[Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x0

    new-array v0, v1, [Ljava/lang/String;

    goto :goto_0
.end method

.method public annotationType()Ljava/lang/Class;
    .locals 1
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "()",
            "Ljava/lang/Class",
            "<+",
            "Ljava/lang/annotation/Annotation;",
            ">;"
        }
    .end annotation

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->annotationType()Ljava/lang/Class;

    move-result-object v0

    return-object v0
.end method

.method public applicationLogFile()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFile:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFile:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->applicationLogFile()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, ""

    goto :goto_0
.end method

.method public applicationLogFileLines()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFileLines:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFileLines:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->applicationLogFileLines()I

    move-result v0

    goto :goto_0

    :cond_1
    const/16 v0, 0x64

    goto :goto_0
.end method

.method public connectionTimeout()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mConnectionTimeout:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mConnectionTimeout:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->connectionTimeout()I

    move-result v0

    goto :goto_0

    :cond_1
    const/16 v0, 0xbb8

    goto :goto_0
.end method

.method public customReportContent()[Lcom/acra/ReportField;
    .locals 2

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mCustomReportContent:[Lcom/acra/ReportField;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mCustomReportContent:[Lcom/acra/ReportField;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->customReportContent()[Lcom/acra/ReportField;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x0

    new-array v0, v1, [Lcom/acra/ReportField;

    goto :goto_0
.end method

.method public deleteOldUnsentReportsOnApplicationStart()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteOldUnsentReportsOnApplicationStart:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteOldUnsentReportsOnApplicationStart:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->deleteOldUnsentReportsOnApplicationStart()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x1

    goto :goto_0
.end method

.method public deleteUnapprovedReportsOnApplicationStart()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteUnapprovedReportsOnApplicationStart:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDeleteUnapprovedReportsOnApplicationStart:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->deleteUnapprovedReportsOnApplicationStart()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x1

    goto :goto_0
.end method

.method public disableSSLCertValidation()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDisableSSLCertValidation:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDisableSSLCertValidation:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->disableSSLCertValidation()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public dropboxCollectionMinutes()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDropboxCollectionMinutes:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mDropboxCollectionMinutes:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->dropboxCollectionMinutes()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x5

    goto :goto_0
.end method

.method public excludeMatchingSettingsKeys()[Ljava/lang/String;
    .locals 2

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSettingsKeys:[Ljava/lang/String;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSettingsKeys:[Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->excludeMatchingSettingsKeys()[Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x0

    new-array v0, v1, [Ljava/lang/String;

    goto :goto_0
.end method

.method public excludeMatchingSharedPreferencesKeys()[Ljava/lang/String;
    .locals 2

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSharedPreferencesKeys:[Ljava/lang/String;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSharedPreferencesKeys:[Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->excludeMatchingSharedPreferencesKeys()[Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x0

    new-array v0, v1, [Ljava/lang/String;

    goto :goto_0
.end method

.method public forceCloseDialogAfterToast()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mForceCloseDialogAfterToast:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mForceCloseDialogAfterToast:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->forceCloseDialogAfterToast()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public formKey()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormKey:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormKey:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->formKey()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, ""

    goto :goto_0
.end method

.method public formUri()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUri:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUri:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->formUri()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, ""

    goto :goto_0
.end method

.method public formUriBasicAuthLogin()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthLogin:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthLogin:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->formUriBasicAuthLogin()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, "ACRA-NULL-STRING"

    goto :goto_0
.end method

.method public formUriBasicAuthPassword()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthPassword:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthPassword:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->formUriBasicAuthPassword()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, "ACRA-NULL-STRING"

    goto :goto_0
.end method

.method public getHttpHeaders()Ljava/util/Map;
    .locals 1
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "()",
            "Ljava/util/Map",
            "<",
            "Ljava/lang/String;",
            "Ljava/lang/String;",
            ">;"
        }
    .end annotation

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mHttpHeaders:Ljava/util/Map;

    return-object v0
.end method

.method public googleFormUrlFormat()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mGoogleFormUrlFormat:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mGoogleFormUrlFormat:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->googleFormUrlFormat()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, "https://docs.google.com/spreadsheet/formResponse?formkey=%s&ifq"

    goto :goto_0
.end method

.method public httpMethod()Lcom/acra/sender/HttpSender$Method;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mHttpMethod:Lcom/acra/sender/HttpSender$Method;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mHttpMethod:Lcom/acra/sender/HttpSender$Method;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->httpMethod()Lcom/acra/sender/HttpSender$Method;

    move-result-object v0

    goto :goto_0

    :cond_1
    sget-object v0, Lcom/acra/sender/HttpSender$Method;->POST:Lcom/acra/sender/HttpSender$Method;

    goto :goto_0
.end method

.method public includeDropBoxSystemTags()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mIncludeDropboxSystemTags:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mIncludeDropboxSystemTags:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->includeDropBoxSystemTags()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public logcatArguments()[Ljava/lang/String;
    .locals 3

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mLogcatArguments:[Ljava/lang/String;

    if-eqz v1, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mLogcatArguments:[Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v1, :cond_1

    iget-object v1, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v1}, Lcom/acra/annotation/ReportsCrashes;->logcatArguments()[Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const/4 v1, 0x4

    new-array v0, v1, [Ljava/lang/String;

    const/4 v1, 0x0

    const-string v2, "-t"

    aput-object v2, v0, v1

    const/4 v1, 0x1

    const/16 v2, 0x64

    invoke-static {v2}, Ljava/lang/Integer;->toString(I)Ljava/lang/String;

    move-result-object v2

    aput-object v2, v0, v1

    const/4 v1, 0x2

    const-string v2, "-v"

    aput-object v2, v0, v1

    const/4 v1, 0x3

    const-string v2, "time"

    aput-object v2, v0, v1

    goto :goto_0
.end method

.method public logcatFilterByPid()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mLogcatFilterByPid:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mLogcatFilterByPid:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->logcatFilterByPid()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public mailTo()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMailTo:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMailTo:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->mailTo()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, ""

    goto :goto_0
.end method

.method public maxNumberOfRequestRetries()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMaxNumberOfRequestRetries:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMaxNumberOfRequestRetries:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->maxNumberOfRequestRetries()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x3

    goto :goto_0
.end method

.method public mode()Lcom/acra/ReportingInteractionMode;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMode:Lcom/acra/ReportingInteractionMode;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mMode:Lcom/acra/ReportingInteractionMode;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->mode()Lcom/acra/ReportingInteractionMode;

    move-result-object v0

    goto :goto_0

    :cond_1
    sget-object v0, Lcom/acra/ReportingInteractionMode;->SILENT:Lcom/acra/ReportingInteractionMode;

    goto :goto_0
.end method

.method public reportType()Lcom/acra/sender/HttpSender$Type;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportType:Lcom/acra/sender/HttpSender$Type;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportType:Lcom/acra/sender/HttpSender$Type;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->reportType()Lcom/acra/sender/HttpSender$Type;

    move-result-object v0

    goto :goto_0

    :cond_1
    sget-object v0, Lcom/acra/sender/HttpSender$Type;->FORM:Lcom/acra/sender/HttpSender$Type;

    goto :goto_0
.end method

.method public resDialogCommentPrompt()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogCommentPrompt:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogCommentPrompt:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogCommentPrompt()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resDialogEmailPrompt()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogEmailPrompt:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogEmailPrompt:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogEmailPrompt()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resDialogIcon()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogIcon:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogIcon:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogIcon()I

    move-result v0

    goto :goto_0

    :cond_1
    const v0, 0x1080027

    goto :goto_0
.end method

.method public resDialogOkToast()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogOkToast:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogOkToast:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogOkToast()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resDialogText()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogText:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogText:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogText()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resDialogTitle()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogTitle:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogTitle:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resDialogTitle()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resNotifIcon()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifIcon:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifIcon:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resNotifIcon()I

    move-result v0

    goto :goto_0

    :cond_1
    const v0, 0x1080078

    goto :goto_0
.end method

.method public resNotifText()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifText:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifText:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resNotifText()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resNotifTickerText()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTickerText:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTickerText:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resNotifTickerText()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resNotifTitle()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTitle:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTitle:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resNotifTitle()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public resToastText()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResToastText:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mResToastText:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->resToastText()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public sendReportsInDevMode()Z
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSendReportsInDevMode:Ljava/lang/Boolean;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSendReportsInDevMode:Ljava/lang/Boolean;

    invoke-virtual {v0}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->sendReportsInDevMode()Z

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x1

    goto :goto_0
.end method

.method public setAdditionalDropboxTags([Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mAdditionalDropboxTags:[Ljava/lang/String;

    return-void
.end method

.method public setAdditionalSharedPreferences([Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mAdditionalSharedPreferences:[Ljava/lang/String;

    return-void
.end method

.method public setApplicationLogFile(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFile:Ljava/lang/String;

    return-void
.end method

.method public setApplicationLogFileLines(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mApplicationLogFileLines:Ljava/lang/Integer;

    return-void
.end method

.method public setConnectionTimeout(Ljava/lang/Integer;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mConnectionTimeout:Ljava/lang/Integer;

    return-void
.end method

.method public setCustomReportContent([Lcom/acra/ReportField;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mCustomReportContent:[Lcom/acra/ReportField;

    return-void
.end method

.method public setDeleteOldUnsentReportsOnApplicationStart(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mDeleteOldUnsentReportsOnApplicationStart:Ljava/lang/Boolean;

    return-void
.end method

.method public setDeleteUnapprovedReportsOnApplicationStart(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mDeleteUnapprovedReportsOnApplicationStart:Ljava/lang/Boolean;

    return-void
.end method

.method public setDisableSSLCertValidation(Z)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Boolean;->valueOf(Z)Ljava/lang/Boolean;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mDisableSSLCertValidation:Ljava/lang/Boolean;

    return-void
.end method

.method public setDropboxCollectionMinutes(Ljava/lang/Integer;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mDropboxCollectionMinutes:Ljava/lang/Integer;

    return-void
.end method

.method public setExcludeMatchingSettingsKeys([Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSettingsKeys:[Ljava/lang/String;

    return-void
.end method

.method public setExcludeMatchingSharedPreferencesKeys([Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mExcludeMatchingSharedPreferencesKeys:[Ljava/lang/String;

    return-void
.end method

.method public setForceCloseDialogAfterToast(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mForceCloseDialogAfterToast:Ljava/lang/Boolean;

    return-void
.end method

.method public setFormKey(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mFormKey:Ljava/lang/String;

    return-void
.end method

.method public setFormUri(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mFormUri:Ljava/lang/String;

    return-void
.end method

.method public setFormUriBasicAuthLogin(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthLogin:Ljava/lang/String;

    return-void
.end method

.method public setFormUriBasicAuthPassword(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mFormUriBasicAuthPassword:Ljava/lang/String;

    return-void
.end method

.method public setHttpHeaders(Ljava/util/Map;)V
    .locals 0
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "(",
            "Ljava/util/Map",
            "<",
            "Ljava/lang/String;",
            "Ljava/lang/String;",
            ">;)V"
        }
    .end annotation

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mHttpHeaders:Ljava/util/Map;

    return-void
.end method

.method public setHttpMethod(Lcom/acra/sender/HttpSender$Method;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mHttpMethod:Lcom/acra/sender/HttpSender$Method;

    return-void
.end method

.method public setIncludeDropboxSystemTags(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mIncludeDropboxSystemTags:Ljava/lang/Boolean;

    return-void
.end method

.method public setLogcatArguments([Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mLogcatArguments:[Ljava/lang/String;

    return-void
.end method

.method public setLogcatFilterByPid(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mLogcatFilterByPid:Ljava/lang/Boolean;

    return-void
.end method

.method public setMailTo(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mMailTo:Ljava/lang/String;

    return-void
.end method

.method public setMaxNumberOfRequestRetries(Ljava/lang/Integer;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mMaxNumberOfRequestRetries:Ljava/lang/Integer;

    return-void
.end method

.method public setMode(Lcom/acra/ReportingInteractionMode;)V
    .locals 0
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Lcom/acra/ACRAConfigurationException;
        }
    .end annotation

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mMode:Lcom/acra/ReportingInteractionMode;

    invoke-static {}, Lcom/acra/ACRA;->checkCrashResources()V

    return-void
.end method

.method public setReportType(Lcom/acra/sender/HttpSender$Type;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mReportType:Lcom/acra/sender/HttpSender$Type;

    return-void
.end method

.method public setResDialogCommentPrompt(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogCommentPrompt:Ljava/lang/Integer;

    return-void
.end method

.method public setResDialogEmailPrompt(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogEmailPrompt:Ljava/lang/Integer;

    return-void
.end method

.method public setResDialogIcon(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogIcon:Ljava/lang/Integer;

    return-void
.end method

.method public setResDialogOkToast(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogOkToast:Ljava/lang/Integer;

    return-void
.end method

.method public setResDialogText(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogText:Ljava/lang/Integer;

    return-void
.end method

.method public setResDialogTitle(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResDialogTitle:Ljava/lang/Integer;

    return-void
.end method

.method public setResNotifIcon(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifIcon:Ljava/lang/Integer;

    return-void
.end method

.method public setResNotifText(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifText:Ljava/lang/Integer;

    return-void
.end method

.method public setResNotifTickerText(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTickerText:Ljava/lang/Integer;

    return-void
.end method

.method public setResNotifTitle(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResNotifTitle:Ljava/lang/Integer;

    return-void
.end method

.method public setResToastText(I)V
    .locals 1

    invoke-static {p1}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;

    move-result-object v0

    iput-object v0, p0, Lcom/acra/ACRAConfiguration;->mResToastText:Ljava/lang/Integer;

    return-void
.end method

.method public setSendReportsInDevMode(Ljava/lang/Boolean;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mSendReportsInDevMode:Ljava/lang/Boolean;

    return-void
.end method

.method public setSharedPreferenceMode(Ljava/lang/Integer;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceMode:Ljava/lang/Integer;

    return-void
.end method

.method public setSharedPreferenceName(Ljava/lang/String;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceName:Ljava/lang/String;

    return-void
.end method

.method public setSocketTimeout(Ljava/lang/Integer;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ACRAConfiguration;->mSocketTimeout:Ljava/lang/Integer;

    return-void
.end method

.method public sharedPreferencesMode()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceMode:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceMode:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->sharedPreferencesMode()I

    move-result v0

    goto :goto_0

    :cond_1
    const/4 v0, 0x0

    goto :goto_0
.end method

.method public sharedPreferencesName()Ljava/lang/String;
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceName:Ljava/lang/String;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSharedPreferenceName:Ljava/lang/String;

    :goto_0
    return-object v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->sharedPreferencesName()Ljava/lang/String;

    move-result-object v0

    goto :goto_0

    :cond_1
    const-string v0, ""

    goto :goto_0
.end method

.method public socketTimeout()I
    .locals 1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSocketTimeout:Ljava/lang/Integer;

    if-eqz v0, :cond_0

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mSocketTimeout:Ljava/lang/Integer;

    invoke-virtual {v0}, Ljava/lang/Integer;->intValue()I

    move-result v0

    :goto_0
    return v0

    :cond_0
    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    if-eqz v0, :cond_1

    iget-object v0, p0, Lcom/acra/ACRAConfiguration;->mReportsCrashes:Lcom/acra/annotation/ReportsCrashes;

    invoke-interface {v0}, Lcom/acra/annotation/ReportsCrashes;->socketTimeout()I

    move-result v0

    goto :goto_0

    :cond_1
    const/16 v0, 0x1388

    goto :goto_0
.end method
