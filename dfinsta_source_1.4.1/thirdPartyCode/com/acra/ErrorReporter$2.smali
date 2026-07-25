.class Lcom/acra/ErrorReporter$2;
.super Ljava/lang/Thread;
.source "ErrorReporter.java"


# annotations
.annotation system Ldalvik/annotation/EnclosingMethod;
    value = Lcom/acra/ErrorReporter;->handleException(Ljava/lang/Throwable;Lcom/acra/ReportingInteractionMode;ZZ)V
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x0
    name = null
.end annotation


# instance fields
.field final synthetic this$0:Lcom/acra/ErrorReporter;


# direct methods
.method constructor <init>(Lcom/acra/ErrorReporter;)V
    .locals 0

    iput-object p1, p0, Lcom/acra/ErrorReporter$2;->this$0:Lcom/acra/ErrorReporter;

    invoke-direct {p0}, Ljava/lang/Thread;-><init>()V

    return-void
.end method


# virtual methods
.method public run()V
    .locals 3

    invoke-static {}, Landroid/os/Looper;->prepare()V

    iget-object v0, p0, Lcom/acra/ErrorReporter$2;->this$0:Lcom/acra/ErrorReporter;

    invoke-static {v0}, Lcom/acra/ErrorReporter;->access$100(Lcom/acra/ErrorReporter;)Landroid/app/Application;

    move-result-object v0

    invoke-static {}, Lcom/acra/ACRA;->getConfig()Lcom/acra/ACRAConfiguration;

    move-result-object v1

    invoke-virtual {v1}, Lcom/acra/ACRAConfiguration;->resToastText()I

    move-result v1

    const/4 v2, 0x1

    invoke-static {v0, v1, v2}, Lcom/acra/util/ToastSender;->sendToast(Landroid/content/Context;II)V

    invoke-static {}, Landroid/os/Looper;->loop()V

    return-void
.end method
