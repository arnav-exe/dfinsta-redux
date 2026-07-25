.class public final enum Lcom/acra/ReportingInteractionMode;
.super Ljava/lang/Enum;
.source "ReportingInteractionMode.java"


# annotations
.annotation system Ldalvik/annotation/Signature;
    value = {
        "Ljava/lang/Enum",
        "<",
        "Lcom/acra/ReportingInteractionMode;",
        ">;"
    }
.end annotation


# static fields
.field private static final synthetic $VALUES:[Lcom/acra/ReportingInteractionMode;

.field public static final enum DIALOG:Lcom/acra/ReportingInteractionMode;

.field public static final enum NOTIFICATION:Lcom/acra/ReportingInteractionMode;

.field public static final enum SILENT:Lcom/acra/ReportingInteractionMode;

.field public static final enum TOAST:Lcom/acra/ReportingInteractionMode;


# direct methods
.method static constructor <clinit>()V
    .locals 6

    const/4 v5, 0x3

    const/4 v4, 0x2

    const/4 v3, 0x1

    const/4 v2, 0x0

    new-instance v0, Lcom/acra/ReportingInteractionMode;

    const-string v1, "SILENT"

    invoke-direct {v0, v1, v2}, Lcom/acra/ReportingInteractionMode;-><init>(Ljava/lang/String;I)V

    sput-object v0, Lcom/acra/ReportingInteractionMode;->SILENT:Lcom/acra/ReportingInteractionMode;

    new-instance v0, Lcom/acra/ReportingInteractionMode;

    const-string v1, "NOTIFICATION"

    invoke-direct {v0, v1, v3}, Lcom/acra/ReportingInteractionMode;-><init>(Ljava/lang/String;I)V

    sput-object v0, Lcom/acra/ReportingInteractionMode;->NOTIFICATION:Lcom/acra/ReportingInteractionMode;

    new-instance v0, Lcom/acra/ReportingInteractionMode;

    const-string v1, "TOAST"

    invoke-direct {v0, v1, v4}, Lcom/acra/ReportingInteractionMode;-><init>(Ljava/lang/String;I)V

    sput-object v0, Lcom/acra/ReportingInteractionMode;->TOAST:Lcom/acra/ReportingInteractionMode;

    new-instance v0, Lcom/acra/ReportingInteractionMode;

    const-string v1, "DIALOG"

    invoke-direct {v0, v1, v5}, Lcom/acra/ReportingInteractionMode;-><init>(Ljava/lang/String;I)V

    sput-object v0, Lcom/acra/ReportingInteractionMode;->DIALOG:Lcom/acra/ReportingInteractionMode;

    const/4 v0, 0x4

    new-array v0, v0, [Lcom/acra/ReportingInteractionMode;

    sget-object v1, Lcom/acra/ReportingInteractionMode;->SILENT:Lcom/acra/ReportingInteractionMode;

    aput-object v1, v0, v2

    sget-object v1, Lcom/acra/ReportingInteractionMode;->NOTIFICATION:Lcom/acra/ReportingInteractionMode;

    aput-object v1, v0, v3

    sget-object v1, Lcom/acra/ReportingInteractionMode;->TOAST:Lcom/acra/ReportingInteractionMode;

    aput-object v1, v0, v4

    sget-object v1, Lcom/acra/ReportingInteractionMode;->DIALOG:Lcom/acra/ReportingInteractionMode;

    aput-object v1, v0, v5

    sput-object v0, Lcom/acra/ReportingInteractionMode;->$VALUES:[Lcom/acra/ReportingInteractionMode;

    return-void
.end method

.method private constructor <init>(Ljava/lang/String;I)V
    .locals 0
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "()V"
        }
    .end annotation

    invoke-direct {p0, p1, p2}, Ljava/lang/Enum;-><init>(Ljava/lang/String;I)V

    return-void
.end method

.method public static valueOf(Ljava/lang/String;)Lcom/acra/ReportingInteractionMode;
    .locals 1

    const-class v0, Lcom/acra/ReportingInteractionMode;

    invoke-static {v0, p0}, Ljava/lang/Enum;->valueOf(Ljava/lang/Class;Ljava/lang/String;)Ljava/lang/Enum;

    move-result-object v0

    check-cast v0, Lcom/acra/ReportingInteractionMode;

    return-object v0
.end method

.method public static values()[Lcom/acra/ReportingInteractionMode;
    .locals 1

    sget-object v0, Lcom/acra/ReportingInteractionMode;->$VALUES:[Lcom/acra/ReportingInteractionMode;

    invoke-virtual {v0}, [Lcom/acra/ReportingInteractionMode;->clone()Ljava/lang/Object;

    move-result-object v0

    check-cast v0, [Lcom/acra/ReportingInteractionMode;

    return-object v0
.end method
