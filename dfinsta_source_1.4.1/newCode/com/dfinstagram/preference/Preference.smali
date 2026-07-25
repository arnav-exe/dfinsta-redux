.class public Lcom/dfinstagram/preference/Preference;
.super Lcom/instagram/base/activity/IgActivity;


# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Lcom/instagram/base/activity/IgActivity;-><init>()V

    return-void
.end method

.method private static getRealPathFromURI(Landroid/content/Context;Landroid/net/Uri;)Ljava/lang/String;
    .locals 10

    const/4 v4, 0x0

    const/4 v1, 0x1

    new-array v3, v1, [Ljava/lang/String;

    const/4 v1, 0x0

    const-string v2, "_data"

    aput-object v2, v3, v1

    new-instance v0, Landroidx/loader/content/CursorLoader;

    move-object v1, p0

    move-object v2, p1

    move-object v5, v4

    move-object v6, v4

    invoke-direct/range {v0 .. v6}, Landroidx/loader/content/CursorLoader;-><init>(Landroid/content/Context;Landroid/net/Uri;[Ljava/lang/String;Ljava/lang/String;[Ljava/lang/String;Ljava/lang/String;)V

    invoke-virtual {v0}, Landroidx/loader/content/CursorLoader;->loadInBackground()Landroid/database/Cursor;

    move-result-object v8

    if-eqz v8, :cond_0

    const-string v1, "_data"

    invoke-interface {v8, v1}, Landroid/database/Cursor;->getColumnIndexOrThrow(Ljava/lang/String;)I

    move-result v7

    invoke-interface {v8}, Landroid/database/Cursor;->moveToFirst()Z

    invoke-interface {v8, v7}, Landroid/database/Cursor;->getString(I)Ljava/lang/String;

    move-result-object v9

    invoke-interface {v8}, Landroid/database/Cursor;->close()V

    :goto_0
    return-object v9

    :cond_0
    const-string v9, "null"

    goto :goto_0
.end method


# virtual methods
.method public final getSession()LX/0aT;
    .locals 1

    invoke-static {}, LX/02T;->A00()LX/0aT;

    move-result-object v0

    return-object v0
.end method

.method public onActivityResult(IILandroid/content/Intent;)V
    .locals 9

    const/4 v8, 0x0

    const/16 v5, 0x3e7

    if-ne p1, v5, :cond_1

    invoke-virtual {p3}, Landroid/content/Intent;->getData()Landroid/net/Uri;

    move-result-object v4

    if-eqz v4, :cond_0

    const-string v5, "com.instagram"

    invoke-virtual {p0, v5, v8}, Lcom/dfinstagram/preference/Preference;->getSharedPreferences(Ljava/lang/String;I)Landroid/content/SharedPreferences;

    move-result-object v2

    invoke-static {p0, v4}, Lcom/hippo/unifile/UniFile;->fromUri(Landroid/content/Context;Landroid/net/Uri;)Lcom/hippo/unifile/UniFile;

    move-result-object v3

    invoke-interface {v2}, Landroid/content/SharedPreferences;->edit()Landroid/content/SharedPreferences$Editor;

    move-result-object v5

    :cond_0
    :goto_0
    invoke-super {p0, p1, p2, p3}, Lcom/instagram/base/activity/IgActivity;->onActivityResult(IILandroid/content/Intent;)V

    return-void

    :cond_1
    const/16 v5, 0x68

    if-ne p1, v5, :cond_0

    if-eqz p3, :cond_0

    invoke-virtual {p3}, Landroid/content/Intent;->getData()Landroid/net/Uri;

    move-result-object v5

    if-eqz v5, :cond_0

    const/4 v5, -0x1

    if-ne p2, v5, :cond_0

    invoke-virtual {p3}, Landroid/content/Intent;->getData()Landroid/net/Uri;

    move-result-object v4

    const/4 v0, 0x3

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getContentResolver()Landroid/content/ContentResolver;

    move-result-object v5

    invoke-virtual {v5, v4, v0}, Landroid/content/ContentResolver;->takePersistableUriPermission(Landroid/net/Uri;I)V

    const-string v5, "com.instagram"

    invoke-virtual {p0, v5, v8}, Lcom/dfinstagram/preference/Preference;->getSharedPreferences(Ljava/lang/String;I)Landroid/content/SharedPreferences;

    move-result-object v2

    invoke-static {p0, v4}, Lcom/hippo/unifile/UniFile;->fromUri(Landroid/content/Context;Landroid/net/Uri;)Lcom/hippo/unifile/UniFile;

    move-result-object v3

    invoke-interface {v2}, Landroid/content/SharedPreferences;->edit()Landroid/content/SharedPreferences$Editor;

    move-result-object v5

    const-string v6, "download_folder"

    invoke-virtual {v3}, Lcom/hippo/unifile/UniFile;->getUri()Landroid/net/Uri;

    move-result-object v7

    invoke-virtual {v7}, Landroid/net/Uri;->toString()Ljava/lang/String;

    move-result-object v7

    invoke-interface {v5, v6, v7}, Landroid/content/SharedPreferences$Editor;->putString(Ljava/lang/String;Ljava/lang/String;)Landroid/content/SharedPreferences$Editor;

    move-result-object v5

    invoke-interface {v5}, Landroid/content/SharedPreferences$Editor;->apply()V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getFragmentManager()Landroid/app/FragmentManager;

    move-result-object v5

    const-string v6, "pref_content"

    invoke-static {v6}, Lcom/dfinstagram/dfinstagram;->getIdEz(Ljava/lang/String;)I

    move-result v6

    invoke-virtual {v5, v6}, Landroid/app/FragmentManager;->findFragmentById(I)Landroid/app/Fragment;

    move-result-object v1

    check-cast v1, Lcom/dfinstagram/preference/PreferenceFragment;

    invoke-virtual {v3}, Lcom/hippo/unifile/UniFile;->getFilePath()Ljava/lang/String;

    move-result-object v5

    invoke-virtual {v1, v5}, Lcom/dfinstagram/preference/PreferenceFragment;->changeFolder(Ljava/lang/String;)V

    new-instance v5, Ljava/lang/StringBuilder;

    invoke-direct {v5}, Ljava/lang/StringBuilder;-><init>()V

    const-string v6, "pref_download_folder"

    invoke-static {v6}, Lcom/dfinstagram/dfinstagram;->getInstanderString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v6

    invoke-virtual {v5, v6}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v5

    const-string v6, ": "

    invoke-virtual {v5, v6}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v5

    invoke-virtual {v3}, Lcom/hippo/unifile/UniFile;->getFilePath()Ljava/lang/String;

    move-result-object v6

    invoke-virtual {v5, v6}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    move-result-object v5

    invoke-virtual {v5}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object v5

    invoke-static {p0, v5, v8}, Landroid/widget/Toast;->makeText(Landroid/content/Context;Ljava/lang/CharSequence;I)Landroid/widget/Toast;

    move-result-object v5

    invoke-virtual {v5}, Landroid/widget/Toast;->show()V

    goto :goto_0
.end method

.method public onBackPressed()V
    .locals 1

    invoke-super {p0}, Lcom/instagram/base/activity/IgActivity;->onBackPressed()V

    sget-boolean v0, Lcom/dfinstagram/dfinstagram;->isrestart:Z

    if-eqz v0, :cond_0

    const/4 v0, 0x0

    invoke-static {v0}, Ljava/lang/System;->exit(I)V

    :cond_0
    return-void
.end method

.method protected onCreate(Landroid/os/Bundle;)V
    .locals 6
    .param p1    # Landroid/os/Bundle;
        .annotation build Landroidx/annotation/Nullable;
        .end annotation
    .end param

    invoke-super {p0, p1}, Lcom/instagram/base/activity/IgActivity;->onCreate(Landroid/os/Bundle;)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getResources()Landroid/content/res/Resources;

    move-result-object v2

    const-string v3, "instander_layout_item"

    const-string v4, "layout"

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getPackageName()Ljava/lang/String;

    move-result-object v5

    invoke-virtual {v2, v3, v4, v5}, Landroid/content/res/Resources;->getIdentifier(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)I

    move-result v2

    invoke-virtual {p0, v2}, Lcom/dfinstagram/preference/Preference;->setContentView(I)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getResources()Landroid/content/res/Resources;

    move-result-object v2

    const-string v3, "action_bar_textview_title"

    const-string v4, "id"

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getPackageName()Ljava/lang/String;

    move-result-object v5

    invoke-virtual {v2, v3, v4, v5}, Landroid/content/res/Resources;->getIdentifier(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)I

    move-result v2

    invoke-virtual {p0, v2}, Lcom/dfinstagram/preference/Preference;->findViewById(I)Landroid/view/View;

    move-result-object v1

    check-cast v1, Landroid/widget/TextView;

    const-string v2, "pref_about_nav_title"

    invoke-static {v2}, Lcom/dfinstagram/dfinstagram;->getInstanderString(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v2

    invoke-virtual {v1, v2}, Landroid/widget/TextView;->setText(Ljava/lang/CharSequence;)V

    invoke-virtual {p0}, Lcom/dfinstagram/preference/Preference;->getFragmentManager()Landroid/app/FragmentManager;

    move-result-object v2

    invoke-virtual {v2}, Landroid/app/FragmentManager;->beginTransaction()Landroid/app/FragmentTransaction;

    move-result-object v2

    const-string v3, "pref_content"

    invoke-static {v3}, Lcom/dfinstagram/dfinstagram;->getIdEz(Ljava/lang/String;)I

    move-result v3

    new-instance v4, Lcom/dfinstagram/preference/PreferenceFragment;

    invoke-direct {v4}, Lcom/dfinstagram/preference/PreferenceFragment;-><init>()V

    invoke-virtual {v2, v3, v4}, Landroid/app/FragmentTransaction;->replace(ILandroid/app/Fragment;)Landroid/app/FragmentTransaction;

    move-result-object v2

    invoke-virtual {v2}, Landroid/app/FragmentTransaction;->commit()I

    return-void
.end method

.method public final onOptionsItemSelected(Landroid/view/MenuItem;)Z
    .locals 2

    invoke-interface {p1}, Landroid/view/MenuItem;->getItemId()I

    move-result v0

    const v1, 0x102002c

    if-ne v0, v1, :cond_0

    invoke-virtual {p0}, Lcom/instagram/base/activity/IgActivity;->onBackPressed()V

    const/4 p1, 0x1

    return p1

    :cond_0
    invoke-super {p0, p1}, Landroid/app/Activity;->onOptionsItemSelected(Landroid/view/MenuItem;)Z

    move-result p1

    return p1
.end method
