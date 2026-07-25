.class public interface abstract Lcom/acra/sender/ReportSender;
.super Ljava/lang/Object;
.source "ReportSender.java"


# virtual methods
.method public abstract send(Lcom/acra/collector/CrashReportData;)V
    .annotation system Ldalvik/annotation/Throws;
        value = {
            Lcom/acra/sender/ReportSenderException;
        }
    .end annotation
.end method
