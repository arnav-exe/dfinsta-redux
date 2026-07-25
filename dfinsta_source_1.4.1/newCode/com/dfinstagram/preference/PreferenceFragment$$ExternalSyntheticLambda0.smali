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
.method public final run()V
    .locals 4

    iget-object v3, p0, Lcom/dfinstagram/preference/PreferenceFragment$$ExternalSyntheticLambda0;->f$0:Lcom/instagram/mainfeed/network/FeedCacheCoordinator;

    const/4 v2, 0x0

    iput-object v2, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A01:LX/1ga;

    const/4 v0, 0x0

    iput-boolean v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A02:Z

    iget-object v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A0B:Lcom/instagram/mainfeed/network/flashfeed/FlashFeedCache;

    if-eqz v0, :cond_0

    iget-object v1, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A09:Lcom/instagram/common/session/UserSession;

    iget-object v0, v0, Lcom/instagram/mainfeed/network/flashfeed/FlashFeedCache;->A04:Ljava/util/LinkedList;

    invoke-virtual {v0}, Ljava/util/AbstractCollection;->clear()V

    sget-object v0, Lcom/instagram/mainfeed/network/flashfeed/FeedItemDatabase;->A00:LX/1h6;

    invoke-static {v1, v0}, LX/1h9;->A01(Lcom/instagram/common/session/UserSession;LX/1h7;)Z

    :cond_0
    iget-object v0, v3, Lcom/instagram/mainfeed/network/FeedCacheCoordinator;->A0A:LX/1gw;

    iput-object v2, v0, LX/1gw;->A00:LX/3KW;

    return-void
.end method
