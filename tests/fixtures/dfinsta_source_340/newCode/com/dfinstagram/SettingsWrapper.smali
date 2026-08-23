.class public Lcom/dfinstagram/SettingsWrapper;
.super Ljava/lang/Object;

# interfaces
.implements Landroid/view/View$OnLongClickListener;
.implements Landroid/content/DialogInterface$OnClickListener;


# direct methods
.method public constructor <init>()V
    .locals 0

    .prologue
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method


# virtual methods
.method public final onClick(Landroid/content/DialogInterface;I)V
    .locals 0

    invoke-static {}, Lcom/dfinstagram/dfinstagram;->startDfInstagramSettings()V

    return-void
.end method

.method public onLongClick(Landroid/view/View;)Z
    .locals 2

    .prologue
    invoke-static {}, Lcom/dfinstagram/dfinstagram;->startDfInstagramSettings()V

    const/4 v0, 0x1

    return v0
.end method
