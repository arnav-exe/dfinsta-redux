.class public Lcom/dfinstagram/preference/PreferenceFragment;
.super Landroid/preference/PreferenceFragment;

# interfaces
.implements Landroid/preference/Preference$OnPreferenceClickListener;
.implements Landroid/content/SharedPreferences$OnSharedPreferenceChangeListener;
.implements Landroid/preference/Preference$OnPreferenceChangeListener;


# static fields
.field static final PATH_L:I = 0x68


# instance fields
.field private context:Landroid/content/Context;

.field private downloadFolder:Landroid/preference/Preference;

.field private updateList:Landroid/preference/Preference;


# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Landroid/preference/PreferenceFragment;-><init>()V

    return-void
.end method

.method static synthetic access$000(Lcom/dfinstagram/preference/PreferenceFragment;)Landroid/content/Context;
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/preference/PreferenceFragment;->context:Landroid/content/Context;

    return-object v0
.end method


# virtual methods
.method public changeFolder(Ljava/lang/String;)V
    .locals 1

    iget-object v0, p0, Lcom/dfinstagram/preference/PreferenceFragment;->downloadFolder:Landroid/preference/Preference;

    invoke-virtual {v0, p1}, Landroid/preference/Preference;->setSummary(Ljava/lang/CharSequence;)V

    return-void
.end method

.method public onCreate(Landroid/os/Bundle;)V
    .locals 5
    .param p1    # Landroid/os/Bundle;
        .annotation build Landroidx/annotation/Nullable;
        .end annotation
    .end param

    invoke-super {p0, p1}, Landroid/preference/PreferenceFragment;->onCreate(Landroid/os/Bundle;)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getPreferenceManager()Landroid/preference/PreferenceManager;

    move-result-object v1

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->SetShared(Landroid/preference/PreferenceManager;)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getResources()Landroid/content/res/Resources;

    move-result-object v1

    const-string v2, "instander_settings"

    const-string v3, "xml"

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getActivity()Landroid/app/Activity;

    move-result-object v4

    invoke-virtual {v4}, Landroid/app/Activity;->getPackageName()Ljava/lang/String;

    move-result-object v4

    invoke-virtual {v1, v2, v3, v4}, Landroid/content/res/Resources;->getIdentifier(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)I

    move-result v1

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->addPreferencesFromResource(I)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getActivity()Landroid/app/Activity;

    move-result-object v1

    iput-object v1, p0, Lcom/dfinstagram/preference/PreferenceFragment;->context:Landroid/content/Context;

    const-string v1, "donate_eth"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceClickListener(Landroid/preference/Preference$OnPreferenceClickListener;)V

    const-string v1, "donate_btc"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceClickListener(Landroid/preference/Preference$OnPreferenceClickListener;)V

    const-string v1, "enable_hardcore"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V


    const-string v1, "disable_feed"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V


    const-string v1, "disable_explore"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V

    const-string v1, "disable_reels"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V

    const-string v1, "disable_shopping"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V

    const-string v1, "disable_stories"

    invoke-virtual {p0, v1}, Lcom/dfinstagram/preference/PreferenceFragment;->findPreference(Ljava/lang/CharSequence;)Landroid/preference/Preference;

    move-result-object v1

    invoke-virtual {v1, p0}, Landroid/preference/Preference;->setOnPreferenceChangeListener(Landroid/preference/Preference$OnPreferenceChangeListener;)V

    return-void
.end method

.method public onPause()V
    .locals 1

    invoke-super {p0}, Landroid/preference/PreferenceFragment;->onPause()V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getPreferenceScreen()Landroid/preference/PreferenceScreen;

    move-result-object v0

    invoke-virtual {v0}, Landroid/preference/PreferenceScreen;->getSharedPreferences()Landroid/content/SharedPreferences;

    move-result-object v0

    invoke-interface {v0, p0}, Landroid/content/SharedPreferences;->unregisterOnSharedPreferenceChangeListener(Landroid/content/SharedPreferences$OnSharedPreferenceChangeListener;)V

    return-void
.end method

.method public onPreferenceClick(Landroid/preference/Preference;)Z
    .locals 14

    invoke-virtual {p1}, Landroid/preference/Preference;->getKey()Ljava/lang/String;

    move-result-object v10

    const/4 v9, -0x1

    invoke-virtual {v10}, Ljava/lang/String;->hashCode()I

    move-result v11

    sparse-switch v11, :sswitch_data_0

    :cond_0
    :goto_0
    packed-switch v9, :pswitch_data_0

    :goto_1
    const/4 v9, 0x1

    return v9

    :sswitch_0
    nop

    goto :goto_0

    :sswitch_1
    const-string v11, "donate_btc"

    invoke-virtual {v10, v11}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v10

    if-eqz v10, :cond_0

    const/4 v9, 0x1

    goto :goto_0

    :sswitch_2
    nop

    goto :goto_0

    :sswitch_3
    nop

    goto :goto_0

    :sswitch_4
    nop

    goto :goto_0

    :sswitch_5
    nop

    goto :goto_0

    :sswitch_6
    nop

    goto :goto_0

    :sswitch_7
    const-string v11, "donate_eth"

    invoke-virtual {v10, v11}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v10

    if-eqz v10, :cond_0

    const/4 v9, 0x7

    goto :goto_0

    :sswitch_8
    nop

    goto :goto_0

    :sswitch_9
    const-string v11, "save_backup"

    invoke-virtual {v10, v11}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v10

    if-eqz v10, :cond_0

    const/16 v9, 0xa

    goto :goto_0

    :sswitch_a
    const-string v11, "restore_backup"

    invoke-virtual {v10, v11}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v10

    if-eqz v10, :cond_0

    const/16 v9, 0xb

    goto :goto_0

    :sswitch_b
    nop

    goto :goto_0

    :sswitch_c
    nop

    goto/16 :goto_0

    nop

    goto/16 :goto_0

    :pswitch_0
    nop

    goto/16 :goto_1

    :pswitch_1
    sget-object v0, Lcom/dfinstagram/startapp;->ctx:Landroid/content/Context;

    const-string v1, "clipboard"

    invoke-virtual {v0, v1}, Landroid/content/Context;->getSystemService(Ljava/lang/String;)Ljava/lang/Object;

    move-result-object v0

    check-cast v0, Landroid/content/ClipboardManager;

    const-string v1, "textlabel"

    const-string v2, "bc1qsr5pyt3acdawzvnez8ly2fyjgpkvksujmjd82r"

    invoke-static {v1, v2}, Landroid/content/ClipData;->newPlainText(Ljava/lang/CharSequence;Ljava/lang/CharSequence;)Landroid/content/ClipData;

    move-result-object v1

    invoke-virtual {v0, v1}, Landroid/content/ClipboardManager;->setPrimaryClip(Landroid/content/ClipData;)V

    const-string v0, "btccopypassdone"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getInstanderString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v0

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->MakeText(Ljava/lang/String;)V

    nop

    goto/16 :goto_1

    :pswitch_2
    nop

    goto/16 :goto_1

    :pswitch_3
    nop

    goto/16 :goto_1

    :pswitch_4
    nop

    goto/16 :goto_1

    :pswitch_5
    nop

    goto/16 :goto_1

    :pswitch_6
    nop

    goto/16 :goto_1

    :pswitch_7
    sget-object v0, Lcom/dfinstagram/startapp;->ctx:Landroid/content/Context;

    const-string v1, "clipboard"

    invoke-virtual {v0, v1}, Landroid/content/Context;->getSystemService(Ljava/lang/String;)Ljava/lang/Object;

    move-result-object v0

    check-cast v0, Landroid/content/ClipboardManager;

    const-string v1, "textlabel"

    const-string v2, "0x4D5Ab9562dfE873d0C3aF59B08F2B651fd5d80dB"

    invoke-static {v1, v2}, Landroid/content/ClipData;->newPlainText(Ljava/lang/CharSequence;Ljava/lang/CharSequence;)Landroid/content/ClipData;

    move-result-object v1

    invoke-virtual {v0, v1}, Landroid/content/ClipboardManager;->setPrimaryClip(Landroid/content/ClipData;)V

    const-string v0, "ethcopypassdone"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getInstanderString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v0

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->MakeText(Ljava/lang/String;)V

    nop

    goto/16 :goto_1

    :pswitch_8
    nop

    goto/16 :goto_1

    :pswitch_9
    nop

    goto/16 :goto_1

    :pswitch_a
    invoke-static {}, Lcom/dfinstagram/backup/PrefsBackupHelper;->performBackup()V

    goto/16 :goto_1

    :pswitch_b
    invoke-static {}, Lcom/dfinstagram/backup/PrefsBackupHelper;->restoreBackup()V

    goto/16 :goto_1

    :pswitch_c
    nop

    goto/16 :goto_1

    :pswitch_d
    nop

    goto/16 :goto_1

    nop

    :sswitch_data_0
    .sparse-switch
        -0x6ac19d17 -> :sswitch_5
        -0x5e049c8e -> :sswitch_8
        -0x4fd494b0 -> :sswitch_6
        -0x28c3bb6d -> :sswitch_a
        -0x2821d85b -> :sswitch_4
        0x30082881 -> :sswitch_1
        0x14227ac4 -> :sswitch_9
        0x2889ba08 -> :sswitch_0
        0x300833c9 -> :sswitch_7
        0x4d30b7ec -> :sswitch_3
        0x5521a6c7 -> :sswitch_2
        0x583cd961 -> :sswitch_c
        0x72c734be -> :sswitch_b
    .end sparse-switch

    :pswitch_data_0
    .packed-switch 0x0
        :pswitch_0
        :pswitch_1
        :pswitch_2
        :pswitch_3
        :pswitch_4
        :pswitch_5
        :pswitch_6
        :pswitch_7
        :pswitch_8
        :pswitch_9
        :pswitch_a
        :pswitch_b
        :pswitch_c
        :pswitch_d
    .end packed-switch
.end method

.method public onResume()V
    .locals 1

    invoke-super {p0}, Landroid/preference/PreferenceFragment;->onResume()V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getPreferenceScreen()Landroid/preference/PreferenceScreen;

    move-result-object v0

    invoke-virtual {v0}, Landroid/preference/PreferenceScreen;->getSharedPreferences()Landroid/content/SharedPreferences;

    move-result-object v0

    invoke-interface {v0, p0}, Landroid/content/SharedPreferences;->registerOnSharedPreferenceChangeListener(Landroid/content/SharedPreferences$OnSharedPreferenceChangeListener;)V

    return-void
.end method

.method public onSharedPreferenceChanged(Landroid/content/SharedPreferences;Ljava/lang/String;)V
    .locals 2

    :goto_0
    const-string v1, "_picker"

    invoke-virtual {p2, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v1

    if-nez v1, :cond_1

    const-string v1, "_check"

    invoke-virtual {p2, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v1

    if-eqz v1, :cond_2

    :cond_1
    const/4 v1, 0x1

    sput-boolean v1, Lcom/dfinstagram/dfinstagram;->isrestart:Z

    :cond_2
    return-void

    :cond_3
    invoke-static {}, Lcom/dfinstagram/followers/Tracker;->getInstance()Lcom/OM7753/gold/followers/Tracker;

    move-result-object v1

    invoke-virtual {v1}, Lcom/dfinstagram/followers/Tracker;->stopService()V

    goto :goto_0
.end method

.method public showFileListerDialog()V
    .locals 0

    return-void

.end method

.method public onPreferenceChange(Landroid/preference/Preference;Ljava/lang/Object;)Z
    .locals 5
    .param p1, "preference"    # Landroid/preference/Preference;
    .param p2, "obj"    # Ljava/lang/Object;
    .annotation system Ldalvik/annotation/MethodParameters;
        accessFlags = {
            0x0,
            0x0
        }
        names = {
            "preference",
            "obj"
        }
    .end annotation

    invoke-virtual {p1}, Landroid/preference/Preference;->getKey()Ljava/lang/String;

    move-result-object v0

    invoke-static {v0}, Lcom/dfinstagram/preference/PreferenceFragment;->isCachedFeature(Ljava/lang/String;)Z

    move-result v4
    .local v4, "cachedFeature":Z
    if-nez v4, :cond_is_hardcore_or_cached

    const-string v3, "enable_hardcore"

    invoke-virtual {v0, v3}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v3

    if-nez v3, :cond_is_hardcore_or_cached

    goto: cond_return_true


    :cond_is_hardcore_or_cached
    move-object v1, p2

    check-cast v1, Ljava/lang/Boolean;

    invoke-virtual {v1}, Ljava/lang/Boolean;->booleanValue()Z

    move-result v1

    .local v1, "isEnabling":Z
    if-nez v1, :cond_enabling_preference

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->isHardcoreModeEnabled()Z

    move-result v1

    .local v1, "hardcoreModeEnabled":Z
    if-eqz v1, :cond_return_true

    const-string v3, "hardcore_mode_blocked_changes"

    invoke-static {v3}, Lcom/dfinstagram/dfinstagram;->getInstanderString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v3

    invoke-static {v3}, Lcom/dfinstagram/dfinstagram;->MakeText(Ljava/lang/String;)V

    goto: cond_return_false

    :cond_enabling_preference

    if-eqz v4, :cond_return_true

    :try_start_0
    const-string v3, "http_responses"

    invoke-virtual {p0, v3}, Lcom/dfinstagram/preference/PreferenceFragment;->deleteCacheDirectory(Ljava/lang/String;)V

    const-string v3, "cold_start"

    invoke-virtual {p0, v3}, Lcom/dfinstagram/preference/PreferenceFragment;->deleteCacheDirectory(Ljava/lang/String;)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->clearFeedDB()V

    :try_end_0
    .catch Ljava/lang/Exception; {:try_start_0 .. :try_end_0} :catch_0

    goto: cond_return_true

    :cond_return_true
    const/4 v2, 0x1
    return v2

    :cond_return_false
    const/4 v2, 0x0
    return v2

    :catch_0
    move-exception v2

    const-string v3, "dfinstagram"

    invoke-static {v3, v2}, Landroid/util/Log;->w(Ljava/lang/String;Ljava/lang/Throwable;)I

    goto: cond_return_true
.end method


.method public isHardcoreModeEnabled()Z
    .locals 3
    invoke-virtual {p0}, Lcom/dfinstagram/preference/PreferenceFragment;->getPreferenceScreen()Landroid/preference/PreferenceScreen;

    move-result-object v0

    invoke-virtual {v0}, Landroid/preference/PreferenceScreen;->getSharedPreferences()Landroid/content/SharedPreferences;

    move-result-object v0

    const-string v1, "enable_hardcore"

    const/4 v2, 0x0

    invoke-interface {v0, v1, v2}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v0

    return v0

.end method

.method public static isCachedFeature(Ljava/lang/String;)Z
    .locals 2
    .param p0, "key"    # Ljava/lang/String;

    const-string v0, "disable_feed"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const-string v0, "disable_explore"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const-string v0, "disable_reels"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const-string v0, "disable_shopping"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const-string v0, "disable_stories"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const-string v0, "disable_suggested_posts"

    invoke-virtual {p0, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z

    move-result v0

    if-nez v0, :cond_1

    const/4 v0, 0x0
    return v0

    :cond_1

    const/4 v0, 0x1
    return v0

.end method



.method public final deleteCacheDirectory(Ljava/lang/String;)V
    .locals 5

    iget-object v0, p0, Lcom/dfinstagram/preference/PreferenceFragment;->context:Landroid/content/Context;

    invoke-virtual {v0}, Landroid/content/Context;->getCacheDir()Ljava/io/File;

    move-result-object v0

    new-instance v1, Ljava/io/File;

    invoke-direct {v1, v0, p1}, Ljava/io/File;-><init>(Ljava/io/File;Ljava/lang/String;)V

    invoke-virtual {v1}, Ljava/io/File;->exists()Z

    move-result v4

    if-eqz v4, :cond_1

    invoke-virtual {v1}, Ljava/io/File;->listFiles()[Ljava/io/File;

    move-result-object v4

    const/4 v0, 0x0

    array-length v1, v4

    :cond_0
    :goto_0
    if-ge v0, v1, :cond_1

    aget-object v2, v4, v0

    add-int/lit8 v0, v0, 0x1

    invoke-virtual {v2}, Ljava/io/File;->isDirectory()Z

    move-result v3

    if-nez v3, :cond_0

    invoke-virtual {v2}, Ljava/io/File;->delete()Z

    goto :goto_0

    :cond_1
    return-void
.end method


.method public final clearFeedDB()V

    .locals 2

    sget-object v0, Lcom/dfinstagram/startapp;->feedCacheCoordinator:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    if-eqz v0, :cond_return

    invoke-static {}, Ljava/util/concurrent/Executors;->newSingleThreadExecutor()Ljava/util/concurrent/ExecutorService;

    move-result-object v1

    new-instance v2, Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;

    invoke-direct {v2, v0}, Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;-><init>(Lcom/instagram/mainfeed/network/FeedCacheCoordinator;)V

    invoke-interface {v1, v2}, Ljava/util/concurrent/ExecutorService;->execute(Ljava/lang/Runnable;)V

    :cond_return
    return-void

.end method