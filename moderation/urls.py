from django.urls import path

from .views import (
    ReportClubMessageView,
    ReportCommentView,
    ReportMessageView,
    ReportStoryView,
    ReportUserView,
    ReportZoneAudioView,
)

urlpatterns = [
    path('report/user/<int:pk>/', ReportUserView.as_view(), name='report-user'),
    path('report/story/<int:pk>/', ReportStoryView.as_view(), name='report-story'),
    path('report/comment/<int:pk>/', ReportCommentView.as_view(), name='report-comment'),
    path('report/message/<int:pk>/', ReportMessageView.as_view(), name='report-message'),
    path('report/club-message/<int:pk>/', ReportClubMessageView.as_view(),
         name='report-club-message'),
    path('report/zone-audio/<int:pk>/', ReportZoneAudioView.as_view(), name='report-zone-audio'),
]
