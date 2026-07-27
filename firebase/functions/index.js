const { onDocumentCreated } = require('firebase-functions/v2/firestore');
const admin = require('firebase-admin');

admin.initializeApp();
const db = admin.firestore();

/**
 * Firestore onCreate trigger: chats/{chatId}/messages/{messageId}
 *
 * Django's ChatConsumer (WebSocket path) sends FCM pushes itself when a
 * message is saved through the REST/WebSocket flow. Messages written
 * directly by a client to Firestore never pass through Django, so this
 * function is the equivalent push trigger for that path.
 */
exports.onNewChatMessage = onDocumentCreated(
  'chats/{chatId}/messages/{messageId}',
  async (event) => {
    const message = event.data.data();
    const { chatId } = event.params;

    const chatSnap = await db.collection('chats').doc(chatId).get();
    if (!chatSnap.exists) return;
    const chat = chatSnap.data();

    const recipientUid = message.sender_id === chat.buyer_id ? chat.seller_id : chat.buyer_id;
    if (!recipientUid) return;

    const userSnap = await db.collection('users').doc(recipientUid).get();
    const fcmToken = userSnap.exists ? userSnap.data().fcm_token : null;
    if (!fcmToken) return;

    try {
      await admin.messaging().send({
        token: fcmToken,
        notification: {
          title: 'رسالة جديدة',
          body: (message.content || '').slice(0, 100),
        },
        data: {
          type: 'new_message',
          chat_id: chatId,
        },
        android: { priority: 'high' },
        apns: { payload: { aps: { sound: 'default' } } },
      });
    } catch (err) {
      console.error('FCM send failed:', err);
    }
  }
);
