.class public final Lcom/dfinstagram/SettingsWrapper;
.super Ljava/lang/Object;

# interfaces
.implements Landroid/view/View$OnLongClickListener;
.implements Landroid/content/DialogInterface$OnMultiChoiceClickListener;

# instance fields
.field private prefs:Landroid/content/SharedPreferences;

# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

# virtual methods
.method public final onLongClick(Landroid/view/View;)Z
    .locals 8

    invoke-virtual {p1}, Landroid/view/View;->getContext()Landroid/content/Context;

    move-result-object v0

    const-string v1, "com.instagram"

    const/4 v2, 0x0

    invoke-virtual {v0, v1, v2}, Landroid/content/Context;->getSharedPreferences(Ljava/lang/String;I)Landroid/content/SharedPreferences;

    move-result-object v1

    iput-object v1, p0, Lcom/dfinstagram/SettingsWrapper;->prefs:Landroid/content/SharedPreferences;

    const/4 v3, 0x5

    new-array v4, v3, [Ljava/lang/CharSequence;

    const-string v5, "Disable feed"

    aput-object v5, v4, v2

    const-string v5, "Disable Explore"

    const/4 v6, 0x1

    aput-object v5, v4, v6

    const-string v5, "Disable Reels"

    const/4 v7, 0x2

    aput-object v5, v4, v7

    const-string v5, "Disable Stories"

    const/4 p1, 0x3

    aput-object v5, v4, p1

    const-string v5, "Disable Shopping"

    const/4 p1, 0x4

    aput-object v5, v4, p1

    new-array v3, v3, [Z

    const-string v5, "disable_feed"

    invoke-interface {v1, v5, v6}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v5

    aput-boolean v5, v3, v2

    const-string v5, "disable_explore"

    invoke-interface {v1, v5, v6}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v5

    aput-boolean v5, v3, v6

    const-string v5, "disable_reels"

    invoke-interface {v1, v5, v6}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v5

    aput-boolean v5, v3, v7

    const-string v5, "disable_stories"

    invoke-interface {v1, v5, v6}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v5

    const/4 v7, 0x3

    aput-boolean v5, v3, v7

    const-string v5, "disable_shopping"

    invoke-interface {v1, v5, v6}, Landroid/content/SharedPreferences;->getBoolean(Ljava/lang/String;Z)Z

    move-result v1

    const/4 v5, 0x4

    aput-boolean v1, v3, v5

    new-instance v1, Landroid/app/AlertDialog$Builder;

    invoke-direct {v1, v0}, Landroid/app/AlertDialog$Builder;-><init>(Landroid/content/Context;)V

    const-string v0, "Distraction-free settings - restart required"

    invoke-virtual {v1, v0}, Landroid/app/AlertDialog$Builder;->setTitle(Ljava/lang/CharSequence;)Landroid/app/AlertDialog$Builder;

    invoke-virtual {v1, v4, v3, p0}, Landroid/app/AlertDialog$Builder;->setMultiChoiceItems([Ljava/lang/CharSequence;[ZLandroid/content/DialogInterface$OnMultiChoiceClickListener;)Landroid/app/AlertDialog$Builder;

    const-string v0, "Close"

    const/4 v2, 0x0

    invoke-virtual {v1, v0, v2}, Landroid/app/AlertDialog$Builder;->setPositiveButton(Ljava/lang/CharSequence;Landroid/content/DialogInterface$OnClickListener;)Landroid/app/AlertDialog$Builder;

    invoke-virtual {v1}, Landroid/app/AlertDialog$Builder;->show()Landroid/app/AlertDialog;


    const/4 v0, 0x1

    return v0
.end method

.method public final onClick(Landroid/content/DialogInterface;IZ)V
    .locals 3

    if-nez p2, :cond_explore

    const-string v0, "disable_feed"

    goto :goto_save

    :cond_explore
    const/4 v0, 0x1

    if-ne p2, v0, :cond_reels

    const-string v0, "disable_explore"

    goto :goto_save

    :cond_reels
    const/4 v0, 0x2

    if-ne p2, v0, :cond_stories

    const-string v0, "disable_reels"

    goto :goto_save

    :cond_stories
    const/4 v0, 0x3

    if-ne p2, v0, :cond_shopping

    const-string v0, "disable_stories"

    goto :goto_save

    :cond_shopping
    const/4 v0, 0x4

    if-ne p2, v0, :cond_return

    const-string v0, "disable_shopping"

    :goto_save
    iget-object v1, p0, Lcom/dfinstagram/SettingsWrapper;->prefs:Landroid/content/SharedPreferences;

    invoke-interface {v1}, Landroid/content/SharedPreferences;->edit()Landroid/content/SharedPreferences$Editor;

    move-result-object v1

    invoke-interface {v1, v0, p3}, Landroid/content/SharedPreferences$Editor;->putBoolean(Ljava/lang/String;Z)Landroid/content/SharedPreferences$Editor;

    move-result-object v1

    invoke-interface {v1}, Landroid/content/SharedPreferences$Editor;->apply()V

    :cond_return
    return-void
.end method
