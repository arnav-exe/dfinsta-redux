.class public final Lcom/dfinstagram/hooks;
.super Ljava/lang/Object;

# direct methods
.method public constructor <init>()V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method public static replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    .locals 1

    const-string v0, "disable_reels"

    invoke-static {v0}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :cond_return_endpoint

    const-string p0, ""

    :cond_return_endpoint
    return-object p0
.end method

.method public static throwIfBlocked(Ljava/net/URI;)V
    .locals 3

    if-eqz p0, :cond_return

    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;

    move-result-object v0

    if-eqz v0, :cond_return

    const-string v1, "/feed/timeline/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_feed_setting

    # contains, not endsWith like the literal above it. Instagram's own matcher
    # over the seven continuous-feed paths (LX/02nZ, read through LX/03g2->A11
    # on 441) is an indexOf >= 0, so the app does not itself assume the path
    # ends at the literal. Nothing else contains this substring, so the looser
    # test costs nothing and the tighter one risks a rule that never fires.
    const-string v1, "/feed/timeline_stream/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v2

    if-eqz v2, :cond_explore

    :cond_feed_setting
    const-string v1, "disable_feed"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_block

    :cond_explore
    const-string v1, "/discover/topical_explore"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v2

    if-eqz v2, :cond_reels_home

    const-string v1, "disable_explore"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_block

    :cond_reels_home
    const-string v1, "/api/v1/clips/homecoming/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_reels_setting

    const-string v1, "/clips/discover"

    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z

    move-result v2

    if-eqz v2, :cond_stories

    :cond_reels_setting
    const-string v1, "disable_reels"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_block

    :cond_stories
    const-string v1, "/feed/reels_tray/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v2

    if-eqz v2, :cond_profile_ads

    const-string v1, "disable_stories"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_block

    :cond_profile_ads
    const-string v1, "/profile_ads/get_profile_ads/"

    invoke-virtual {v0, v1}, Ljava/lang/String;->endsWith(Ljava/lang/String;)Z

    move-result v2

    if-eqz v2, :cond_return

    const-string v1, "disable_adds"

    invoke-static {v1}, Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z

    move-result v2

    if-nez v2, :cond_block

    :cond_return
    return-void

    :cond_block
    new-instance v0, Ljava/io/IOException;

    const-string v1, "Blocked by DFInsta setting"

    invoke-direct {v0, v1}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V

    throw v0
.end method
