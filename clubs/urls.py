from django.urls import path
from . import views

urlpatterns = [
    path('', views.ClubListCreateView.as_view(), name='club-list-create'),
    path('<int:pk>/', views.ClubDetailView.as_view(), name='club-detail'),
    path('<int:pk>/join/', views.ClubJoinView.as_view(), name='club-join'),
    path('<int:pk>/leave/', views.ClubLeaveView.as_view(), name='club-leave'),
    path('<int:pk>/invite/<int:user_id>/', views.ClubInviteView.as_view(), name='club-invite'),
    path('<int:pk>/kick/<int:user_id>/', views.ClubKickView.as_view(), name='club-kick'),
    path('<int:pk>/promote/<int:user_id>/', views.ClubPromoteView.as_view(), name='club-promote'),
    path('<int:pk>/transfer/<int:user_id>/', views.ClubTransferView.as_view(), name='club-transfer'),
    path('<int:pk>/members/', views.ClubMembersView.as_view(), name='club-members'),
]
