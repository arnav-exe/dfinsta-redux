.class public final synthetic Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;
.super Ljava/lang/Object;
.source "D8$$SyntheticClass"

# interfaces
.implements Ljava/lang/Runnable;

# instance fields
.field public final synthetic f$0:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;


# direct methods
.method public synthetic constructor <init>(Lcom/instagram/mainfeed/network/FeedCacheCoordinator;)V
    .locals 0

    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    iput-object p1, p0, Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;->f$0:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    return-void
.end method


# virtual methods
# Copied from 11z.smali. Control of the feed cache was moved outside in the last IG refactor for whatever reason.
.method public final run()V
    .locals 4

    iget-object v3, p0, Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;->f$0:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    const/4 v2, 0x0

    iput-object v2, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A01:LX/2EM;

    const/4 v0, 0x0

    iput-boolean v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A02:Z

    iget-object v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A08:Lcom/instagram/mainfeed/network/FlashFeedCache;

    if-eqz v0, :cond_3

    iget-object v1, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A09:LX/0bd;

    iget-object v0, v0, Lcom/instagram/mainfeed/network/FlashFeedCache;->A05:Ljava/util/LinkedList;

    invoke-virtual {v0}, Ljava/util/AbstractCollection;->clear()V

    sget-object v0, Lcom/instagram/mainfeed/network/FeedItemDatabase;->A00:LX/2Fo;

    invoke-static {v0, v1}, LX/1fn;->A01(LX/1fh;LX/0bd;)Z

    :cond_3
    iget-object v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A07:LX/1DP;

    iput-object v2, v0, LX/1DP;->A00:LX/2tl;

    return-void
.end method
