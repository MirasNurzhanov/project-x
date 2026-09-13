from django.urls import path
from . import views

urlpatterns = [
    path('', views.ClubListCreateView.as_view(), name='club-list-create'),
    # Before the <int:pk> route so the word is never read as an id.
    path('my-location/', views.MyClubLocationView.as_view(), name='club-my-location'),
    path('invitations/', views.ClubInvitationListView.as_view(), name='club-invitations'),
    path('invitations/<int:pk>/accept/', views.ClubInvitationAcceptView.as_view(), name='club-invitation-accept'),
    path('invitations/<int:pk>/decline/', views.ClubInvitationDeclineView.as_view(), name='club-invitation-decline'),
    path('<int:pk>/', views.ClubDetailView.as_view(), name='club-detail'),
    path('<int:pk>/join/', views.ClubJoinView.as_view(), name='club-join'),
    path('<int:pk>/leave/', views.ClubLeaveView.as_view(), name='club-leave'),
    path('<int:pk>/invite/<int:user_id>/', views.ClubInviteView.as_view(), name='club-invite'),
    path('<int:pk>/kick/<int:user_id>/', views.ClubKickView.as_view(), name='club-kick'),
    path('<int:pk>/promote-owner/<int:user_id>/', views.ClubPromoteOwnerView.as_view(), name='club-promote-owner'),
    path('<int:pk>/members/', views.ClubMembersView.as_view(), name='club-members'),
    path('<int:pk>/messages/', views.ClubMessageListView.as_view(), name='club-messages'),
    path('<int:pk>/events/', views.ClubEventListCreateView.as_view(), name='club-event-list-create'),
    path('events/nearby/', views.ClubEventNearbyView.as_view(), name='club-event-nearby'),
    path('events/<int:event_id>/', views.ClubEventDetailView.as_view(), name='club-event-detail'),
    path('events/<int:event_id>/approve/', views.ClubEventApproveView.as_view(), name='club-event-approve'),
    path('events/<int:event_id>/reject/', views.ClubEventRejectView.as_view(), name='club-event-reject'),
]
