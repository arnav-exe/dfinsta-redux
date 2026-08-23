.class public Lcom/dfinstagram/hooks;
.super Ljava/lang/Object;


# static fields
.field public static final TAG:Ljava/lang/String; = "instamod"

# direct methods
.method public constructor <init>()V
    .locals 0

    .prologue
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static handleStartActivity(Landroid/app/Activity;Landroid/content/Intent;)Landroid/content/Intent;
    .locals 0

    .prologue
    return-object p1
.end method

.method public static openBrowserThatDoesNotSuck(Landroid/app/Activity;Landroid/content/Intent;)V
    .locals 3

    .prologue
    invoke-virtual {p1}, Landroid/content/Intent;->getData()Landroid/net/Uri;

    move-result-object v0

    const-string v1, "u"

    invoke-virtual {v0, v1}, Landroid/net/Uri;->getQueryParameter(Ljava/lang/String;)Ljava/lang/String;

    move-result-object v0

    if-eqz v0, :cond_0

    new-instance v1, Landroid/content/Intent;

    const-string v2, "android.intent.action.VIEW"

    invoke-static {v0}, Landroid/net/Uri;->parse(Ljava/lang/String;)Landroid/net/Uri;

    move-result-object v0

    invoke-direct {v1, v2, v0}, Landroid/content/Intent;-><init>(Ljava/lang/String;Landroid/net/Uri;)V

    invoke-virtual {p0, v1}, Landroid/app/Activity;->startActivity(Landroid/content/Intent;)V

    :cond_0
    invoke-virtual {p0}, Landroid/app/Activity;->finish()V

    const/4 v0, 0x0

    invoke-static {v0}, Ljava/lang/System;->exit(I)V

    return-void
.end method

.method public static throwIfBlocked(Ljava/net/URI;)V
    .locals 4

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/feed/timeline/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-eqz v3, :cond_0

    const-string v0, "disable_feed"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_6

    :cond_0
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/discover/topical_explore"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_1

    const-string v0, "disable_explore"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_6

    :cond_1
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/qp/batch_fetch/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-nez v3, :cond_2

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/api/v1/clips/home/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-nez v3, :cond_2

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/clips/discover"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_3

    :cond_2
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_6

    :cond_3
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "/feed/reels_tray/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v3

    if-eqz v3, :cond_4

    const-string v0, "disable_stories"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_6

    :cond_4
    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    const-string v1, "minishop"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v3

    if-eqz v3, :cond_5

    const-string v0, "disable_shopping"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :cond_6

    :cond_5
    return-void

    :cond_6
    new-instance v2, Ljava/io/IOException;

    const-string v0, "URL has no host"

    invoke-direct {v2, v0}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V

    throw v2
.end method
