.class public Lcom/dfinstagram/DistractionFree;
.super Ljava/lang/Object;
.source "DistractionFree.java"


# direct methods
.method public constructor <init>()V
    .locals 0

    .prologue
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static improveRemoveAdsProfile()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_adds"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "profile_ads/get_profile_ads/"

    goto :goto_0
.end method

.method public static improveRemoveExplore()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_explore"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "discover/topical_explore/"

    goto :goto_0
.end method

.method public static improveRemoveExploreStream()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_explore"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "discover/topical_explore_stream/"

    goto :goto_0
.end method

.method public static improveRemovePosts()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_feed"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "feed/timeline/"

    goto :goto_0
.end method

.method public static improveRemoveReels()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "clips/discover/"

    goto :goto_0
.end method

.method public static improveRemoveReelsMixed()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "mixed_media/discover/"

    goto :goto_0
.end method

.method public static improveRemoveReelsMixedStream()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "mixed_media/discover/stream/"

    goto :goto_0
.end method

.method public static improveRemoveReelsStream()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "clips/discover/stream/"

    goto :goto_0
.end method

.method public static improveRemoveShopping(Ljava/lang/String;)Ljava/lang/String;
    .locals 2

    .prologue
    const-string v0, "disable_shopping"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v1, "minshop"

    invoke-virtual {p0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    return-object v0

    :cond_0
    return-object p0
.end method

.method public static improveRemoveStories()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_stories"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "feed/reels_tray/"

    goto :goto_0
.end method

.method public static improveRemoveStoriesV1()Ljava/lang/String;
    .locals 1

    .prologue
    const-string v0, "disable_stories"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_0

    const-string v0, ""

    :goto_0
    return-object v0

    :cond_0
    const-string v0, "feed/reels_tray/_v1"

    goto :goto_0
.end method
