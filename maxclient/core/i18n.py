"""Opt-in local UI translations; never reads account data or changes transport locale.

Call tr() only for application-owned source strings. It deliberately does not
walk widgets, replace arbitrary text, translate names/messages or run JavaScript.
Unknown phrases fall back to their original Russian source.
"""
from __future__ import annotations

from functools import lru_cache

from ._i18n_catalog import OFFICIAL
from ._folder_editor_translations import FOLDER_EDITOR_TRANSLATIONS
from ._contact_dialog_translations import CONTACT_DIALOG_TRANSLATIONS
from ._settings_dialog_translations import SETTINGS_DIALOG_TRANSLATIONS
from ._message_action_translations import MESSAGE_ACTION_TRANSLATIONS
from ._contacts_workspace_translations import CONTACTS_WORKSPACE_TRANSLATIONS
from ._conversation_management_translations import CONVERSATION_MANAGEMENT_TRANSLATIONS
from ._conversation_info_translations import CONVERSATION_INFO_TRANSLATIONS
from ._chat_row_reason_translations import CHAT_ROW_REASON_TRANSLATIONS
from ._contact_editor_translations import CONTACT_EDITOR_TRANSLATIONS
from ._conversation_create_translations import CONVERSATION_CREATE_TRANSLATIONS
from ._file_download_translations import FILE_DOWNLOAD_TRANSLATIONS
from ._photo_viewer_translations import PHOTO_VIEWER_TRANSLATIONS
from ._search_translations import SEARCH_TRANSLATIONS
from ._codes_dialog_translations import CODES_DIALOG_TRANSLATIONS

SUPPORTED_LANGUAGES = ('ru', 'en', 'es', 'pt', 'fr', 'uz', 'be')
LANGUAGE_TITLES = ('Русский', 'English', 'Español', 'Português (Brasil)', 'Французский', 'O‘zbekcha', 'Беларуская')
LANGUAGE_SUBTITLES = ('Русский', 'Английский', 'Испанский', 'Португальский (Бразилия)', 'Французский', 'Узбекский', 'Белорусский')

# These client-specific phrases are not claimed to be official MAX translations.
# Columns follow en, es, pt-BR, fr, uz, be. The public strings are in the separate
# generated catalogue and retain their source provenance.
_OWN = {
    'MAX принял команду. Результат удаления ещё проверяется. Выберите «Проверить состояние»; повторное удаление не отправляется.': (
        'MAX accepted the command. The deletion result is still being checked. Select “Check status”; no duplicate deletion is sent.',
        'MAX aceptó la orden. El resultado de la eliminación sigue pendiente de comprobación. Selecciona «Comprobar estado»; no se envía otra eliminación.',
        'O MAX aceitou o comando. O resultado da exclusão ainda está sendo verificado. Selecione “Verificar status”; nenhuma exclusão duplicada é enviada.',
        'MAX a accepté la commande. Le résultat de la suppression est encore en cours de vérification. Sélectionnez « Vérifier l’état » ; aucune nouvelle suppression n’est envoyée.',
        'MAX buyruqni qabul qildi. O‘chirish natijasi hali tekshirilmoqda. «Holatni tekshirish»ni tanlang; takroriy o‘chirish yuborilmaydi.',
        'MAX прыняў каманду. Вынік выдалення яшчэ правяраецца. Выберыце «Праверыць стан»; паўторнае выдаленне не адпраўляецца.'),
    'Изменение не выполнено. Обновите список перед новой попыткой.': (
        'The change was not applied. Refresh the list before trying again.',
        'No se aplicó el cambio. Actualiza la lista antes de volver a intentarlo.',
        'A alteração não foi aplicada. Atualize a lista antes de tentar novamente.',
        'La modification n’a pas été appliquée. Actualisez la liste avant de réessayer.',
        'O‘zgarish qo‘llanmadi. Qayta urinishdan oldin ro‘yxatni yangilang.',
        'Змена не ўжыта. Абнавіце спіс перад новай спробай.'),
    'Порядок папок': ('Folder order', 'Orden de carpetas', 'Ordem das pastas',
        'Ordre des dossiers', 'Jildlar tartibi', 'Парадак папак'),
    'Перетащите папку, чтобы изменить порядок. Можно также использовать кнопки «Вверх» и «Вниз».': (
        'Drag a folder to change its position. You can also use the Up and Down buttons.',
        'Arrastra una carpeta para cambiar su posición. También puedes usar los botones «Arriba» y «Abajo».',
        'Arraste uma pasta para alterar a posição. Você também pode usar os botões «Para cima» e «Para baixo».',
        'Faites glisser un dossier pour modifier sa position. Vous pouvez aussi utiliser les boutons « Monter » et « Descendre ».',
        'Tartibni o‘zgartirish uchun jildni sudrang. «Yuqoriga» va «Pastga» tugmalaridan ham foydalanishingiz mumkin.',
        'Перацягніце папку, каб змяніць парадак. Можна таксама выкарыстоўваць кнопкі «Уверх» і «Уніз».'),
    # This shared privacy choice also applies to calls and invitations; the
    # retained Uzbek catalogue wording refers specifically to finding someone.
    # Preserve the other five translations and use a neutral local correction.
    'могут контакты': ('contacts can', 'solo los contactos pueden', 'contatos podem',
        'mes contacts', 'faqat kontaktlar', 'могуць кантакты'),
    # Correct the retained French spelling locally; other columns are unchanged.
    'Видеть статус «в сети»': ('See online status', 'Ver el estado «en línea»', 'Ver status “online”',
        'Voir le statut en ligne', '"Onlayn" holatini ko‘rish', 'Бачыць статус «у сетцы»'),
    'MAX пока не передал настройки.': ('MAX has not provided settings yet.',
        'MAX aún no ha proporcionado los ajustes.', 'O MAX ainda não enviou as configurações.',
        'MAX n’a pas encore transmis les paramètres.', 'MAX sozlamalarni hali yubormadi.',
        'MAX пакуль не перадаў налады.'),
    'никто (текущее значение)': ('no one (current value)', 'nadie (valor actual)',
        'ninguém (valor atual)', 'personne (valeur actuelle)', 'hech kim (joriy qiymat)',
        'ніхто (бягучае значэнне)'),
    'Состояние настроек приватности': ('Privacy settings status', 'Estado de los ajustes de privacidad',
        'Estado das configurações de privacidade', 'État des paramètres de confidentialité',
        'Maxfiylik sozlamalari holati', 'Стан налад прыватнасці'),
    'Состояние настроек безопасности': ('Security settings status', 'Estado de los ajustes de seguridad',
        'Estado das configurações de segurança', 'État des paramètres de sécurité',
        'Xavfsizlik sozlamalari holati', 'Стан налад бяспекі'),
    'Почта: {email}': ('Email: {email}', 'Correo: {email}', 'E-mail: {email}',
        'E-mail : {email}', 'Elektron pochta: {email}', 'Пошта: {email}'),
    'Не удалось загрузить состояние пароля. Обновите статус в разделе «Безопасность».': (
        'Could not load password status. Refresh the status in Security.',
        'No se pudo cargar el estado de la contraseña. Actualiza el estado en «Seguridad».',
        'Não foi possível carregar o estado da senha. Atualize o estado em «Segurança».',
        'Impossible de charger l’état du mot de passe. Actualisez l’état dans « Sécurité ».',
        'Parol holatini yuklab bo‘lmadi. «Xavfsizlik» bo‘limida holatni yangilang.',
        'Не ўдалося загрузіць стан пароля. Абнавіце стан у раздзеле «Бяспека».'),
    'Уведомления и звук': ('Notifications and sound', 'Notificaciones y sonido', 'Notificações e som',
        'Notifications et son', 'Bildirishnomalar va ovoz', 'Апавяшчэнні і гук'),
    'Личные чаты': ('Personal chats', 'Chats personales', 'Conversas pessoais',
        'Discussions personnelles', 'Shaxsiy chatlar', 'Асабістыя чаты'),
    'Настройки личных чатов пока недоступны в этом клиенте.': (
        'Personal chat settings are not available in this client yet.',
        'Los ajustes de chats personales aún no están disponibles en este cliente.',
        'As configurações de conversas pessoais ainda não estão disponíveis neste cliente.',
        'Les paramètres des discussions personnelles ne sont pas encore disponibles dans ce client.',
        'Bu ilovada shaxsiy chat sozlamalari hozircha mavjud emas.',
        'Налады асабістых чатаў пакуль недаступныя ў гэтым кліенце.'),
    'Настройки уведомлений каналов пока недоступны в этом клиенте.': (
        'Channel notification settings are not available in this client yet.',
        'Los ajustes de notificaciones de canales aún no están disponibles en este cliente.',
        'As configurações de notificações de canais ainda não estão disponíveis neste cliente.',
        'Les paramètres de notification des chaînes ne sont pas encore disponibles dans ce client.',
        'Bu ilovada kanal bildirishnomalari sozlamalari hozircha mavjud emas.',
        'Налады апавяшчэнняў каналаў пакуль недаступныя ў гэтым кліенце.'),
    'Эта настройка пока недоступна в этом клиенте.': (
        'This setting is not available in this client yet.',
        'Este ajuste aún no está disponible en este cliente.',
        'Esta configuração ainda não está disponível neste cliente.',
        'Ce paramètre n’est pas encore disponible dans ce client.',
        'Bu ilovada ushbu sozlama hozircha mavjud emas.',
        'Гэта налада пакуль недаступная ў гэтым кліенце.'),
    'Сброс настроек уведомлений пока недоступен в этом клиенте.': (
        'Resetting notification settings is not available in this client yet.',
        'Aún no se pueden restablecer los ajustes de notificaciones en este cliente.',
        'Ainda não é possível redefinir as configurações de notificação neste cliente.',
        'La réinitialisation des paramètres de notification n’est pas encore disponible dans ce client.',
        'Bu ilovada bildirishnoma sozlamalarini tiklash hozircha mavjud emas.',
        'Скід налад апавяшчэнняў пакуль недаступны ў гэтым кліенце.'),
    'Выбор уведомлений об ответах пока недоступен в этом клиенте.': (
        'Selecting notifications for replies is not available in this client yet.',
        'Aún no se pueden seleccionar las notificaciones de respuestas en este cliente.',
        'Ainda não é possível selecionar notificações de respostas neste cliente.',
        'Le choix des notifications de réponse n’est pas encore disponible dans ce client.',
        'Bu ilovada javoblar haqidagi bildirishnomalarni tanlash hozircha mavjud emas.',
        'Выбар апавяшчэнняў пра адказы пакуль недаступны ў гэтым кліенце.'),
    'Показывать текст сообщений': ('Show message text', 'Mostrar el texto de los mensajes',
        'Mostrar o texto das mensagens', 'Afficher le texte des messages',
        'Xabarlar matnini ko‘rsatish', 'Паказваць тэкст паведамленняў'),
    'Не беспокоить во время звонка': ('Do not disturb during calls', 'No molestar durante las llamadas',
        'Não perturbar durante chamadas', 'Ne pas déranger pendant les appels',
        'Qo‘ng‘iroq paytida bezovta qilmaslik', 'Не турбаваць падчас званка'),
    'Уведомлять о новых контактах': ('Notify about new contacts', 'Notificar sobre nuevos contactos',
        'Notificar sobre novos contatos', 'Signaler les nouveaux contacts',
        'Yangi kontaktlar haqida xabar berish', 'Апавяшчаць пра новыя кантакты'),
    'Сбросить настройки уведомлений': ('Reset notification settings', 'Restablecer ajustes de notificaciones',
        'Redefinir configurações de notificação', 'Réinitialiser les paramètres de notification',
        'Bildirishnoma sozlamalarini tiklash', 'Скінуць налады апавяшчэнняў'),
    'Уведомления на этом компьютере': ('Notifications on this computer', 'Notificaciones en este ordenador',
        'Notificações neste computador', 'Notifications sur cet ordinateur',
        'Bu kompyuterdagi bildirishnomalar', 'Апавяшчэнні на гэтым камп’ютары'),
    'Состояние настроек уведомлений': ('Notification settings status', 'Estado de los ajustes de notificaciones',
        'Estado das configurações de notificação', 'État des paramètres de notification',
        'Bildirishnoma sozlamalari holati', 'Стан налад апавяшчэнняў'),
    'ПОЛУЧАТЬ УВЕДОМЛЕНИЯ': ('NOTIFY ME ABOUT', 'RECIBIR NOTIFICACIONES', 'RECEBER NOTIFICAÇÕES',
        'RECEVOIR DES NOTIFICATIONS', 'BILDIRISHNOMALAR OLISH', 'АТРЫМЛІВАЦЬ АПАВЯШЧЭННІ'),
    'Обо всём': ('Everything', 'Todo', 'Tudo', 'Tout', 'Hammasi haqida', 'Пра ўсё'),
    'Об ответах': ('Replies', 'Respuestas', 'Respostas', 'Les réponses', 'Javoblar haqida', 'Пра адказы'),
    'О новых постах': ('New posts', 'Publicaciones nuevas', 'Novas publicações',
        'Les nouvelles publications', 'Yangi postlar haqida', 'Пра новыя допісы'),
    'Об ответах на ваши комментарии': ('Replies to your comments', 'Respuestas a tus comentarios',
        'Respostas aos seus comentários', 'Les réponses à vos commentaires',
        'Izohlaringizga javoblar haqida', 'Пра адказы на вашы каментарыі'),
    'Включены': ('On', 'Activadas', 'Ativadas', 'Activées', 'Yoqilgan', 'Уключаныя'),
    'Выключены': ('Off', 'Desactivadas', 'Desativadas', 'Désactivées', 'O‘chirilgan', 'Выключаныя'),
    'Изменение ещё не подтверждено MAX.': ('MAX has not confirmed the change yet.',
        'MAX aún no ha confirmado el cambio.', 'O MAX ainda não confirmou a alteração.',
        'MAX n’a pas encore confirmé la modification.', 'MAX o‘zgarishni hali tasdiqlamadi.',
        'MAX яшчэ не пацвердзіў змяненне.'),
    'MAX пока не передал настройки уведомлений.': ('MAX has not provided notification settings yet.',
        'MAX aún no ha proporcionado los ajustes de notificaciones.',
        'O MAX ainda não enviou as configurações de notificação.',
        'MAX n’a pas encore transmis les paramètres de notification.',
        'MAX bildirishnoma sozlamalarini hali yubormadi.',
        'MAX пакуль не перадаў налады апавяшчэнняў.'),
    'Не удалось подтвердить настройки. Нажмите «Обновить», чтобы проверить состояние.': (
        'Could not confirm the settings. Click Update to check their status.',
        'No se pudieron confirmar los ajustes. Pulsa «Actualizar» para comprobar su estado.',
        'Não foi possível confirmar as configurações. Clique em «Atualizar» para verificar o estado.',
        'Impossible de confirmer les paramètres. Cliquez sur « Actualiser » pour vérifier leur état.',
        'Sozlamalarni tasdiqlab bo‘lmadi. Holatni tekshirish uchun «Yangilash»ni bosing.',
        'Не ўдалося пацвердзіць налады. Націсніце «Абнавіць», каб праверыць стан.'),
    # The retained French catalogue value was the untranslated Russian source.
    # Keep its existing translations in other columns; this correction is local.
    'Обновить': ('Update', 'Actualizar', 'Atualizar', 'Actualiser', 'Yangilash', 'Абнавіць'),
    'Звук уведомлений': ('Notification sound', 'Sonido de notificaciones', 'Som das notificações', 'Son des notifications', 'Bildirishnoma ovozi', 'Гук апавяшчэнняў'),
    'Звук в группах': ('Group notification sound', 'Sonido en grupos', 'Som em grupos', 'Son dans les groupes', 'Guruhlardagi ovoz', 'Гук у групах'),
    'При включении выбирается стандартный звук MAX.': (
        'Turning this on selects the default MAX sound.', 'Al activar esta opción se selecciona el sonido predeterminado de MAX.',
        'Ativar esta opção seleciona o som padrão do MAX.', 'L’activation sélectionne le son par défaut de MAX.',
        'Yoqilganda MAX standart ovozi tanlanadi.', 'Пры ўключэнні выбіраецца стандартны гук MAX.'),
    'В MAX сохранена другая настройка звука. Здесь используется стандартный звук; повторное включение выберет его и в MAX.': (
        'MAX has a different sound setting saved. The default sound is used here; turning sound back on will also select it in MAX.',
        'MAX tiene guardada otra configuración de sonido. Aquí se usa el sonido predeterminado; al volver a activar el sonido también se seleccionará en MAX.',
        'O MAX tem outra configuração de som salva. Aqui é usado o som padrão; reativar o som também o selecionará no MAX.',
        'MAX conserve un autre réglage sonore. Le son par défaut est utilisé ici ; réactiver le son le sélectionnera aussi dans MAX.',
        'MAX’da boshqa ovoz sozlamasi saqlangan. Bu yerda standart ovoz ishlatiladi; ovozni qayta yoqish MAX’da ham uni tanlaydi.',
        'У MAX захавана іншая налада гуку. Тут выкарыстоўваецца стандартны гук; паўторнае ўключэнне выбера яго і ў MAX.'),
    'Настройка звука пока неизвестна.': ('The sound setting is not known yet.', 'La configuración de sonido aún se desconoce.', 'A configuração de som ainda é desconhecida.', 'Le réglage sonore est encore inconnu.', 'Ovoz sozlamasi hali noma’lum.', 'Налада гуку пакуль невядомая.'),
    'Не удалось сохранить настройку на этом компьютере.': (
        'Could not save the setting on this computer.', 'No se pudo guardar la configuración en este ordenador.',
        'Não foi possível salvar a configuração neste computador.', 'Impossible d’enregistrer le réglage sur cet ordinateur.',
        'Bu kompyuterda sozlamani saqlab bo‘lmadi.', 'Не ўдалося захаваць наладу на гэтым камп’ютары.'),
    'Изменить сведения': ('Edit details', 'Editar información', 'Editar informações', 'Modifier les informations', 'Ma’lumotlarni tahrirlash', 'Змяніць звесткі'),
    'Название': ('Title', 'Nombre', 'Nome', 'Nom', 'Nomi', 'Назва'),
    'Описание': ('Description', 'Descripción', 'Descrição', 'Description', 'Tavsif', 'Апісанне'),
    'Введите название.': ('Enter a title.', 'Introduce un nombre.', 'Digite um nome.', 'Saisissez un nom.', 'Nomni kiriting.', 'Увядзіце назву.'),
    'Название слишком длинное.': ('The title is too long.', 'El nombre es demasiado largo.', 'O nome é muito longo.', 'Le nom est trop long.', 'Nom juda uzun.', 'Назва занадта доўгая.'),
    'Описание слишком длинное.': ('The description is too long.', 'La descripción es demasiado larga.', 'A descrição é muito longa.', 'La description est trop longue.', 'Tavsif juda uzun.', 'Апісанне занадта доўгае.'),
    'Название и описание будут изменены для всех участников.': (
        'The title and description will change for all members.', 'El nombre y la descripción cambiarán para todos los miembros.',
        'O nome e a descrição serão alterados para todos os membros.', 'Le nom et la description seront modifiés pour tous les membres.',
        'Nom va tavsif barcha ishtirokchilar uchun o‘zgaradi.', 'Назва і апісанне будуць зменены для ўсіх удзельнікаў.'),
    'Загрузка сведений…': ('Loading details…', 'Cargando información…', 'Carregando informações…', 'Chargement des informations…', 'Ma’lumotlar yuklanmoqda…', 'Загрузка звестак…'),
    'Обновить сведения': ('Refresh details', 'Actualizar información', 'Atualizar informações', 'Actualiser les informations', 'Ma’lumotlarni yangilash', 'Абнавіць звесткі'),
    'Пустое описание удалит прежнее описание.': (
        'An empty description will remove the previous description.', 'Una descripción vacía eliminará la descripción anterior.',
        'Uma descrição vazia removerá a descrição anterior.', 'Une description vide supprimera la description précédente.',
        'Bo‘sh tavsif oldingi tavsifni o‘chiradi.', 'Пустое апісанне выдаліць ранейшае апісанне.'),
    '{count} из {limit} (эмодзи могут занимать два символа)': (
        '{count} of {limit} (emoji may count as two characters)', '{count} de {limit} (los emojis pueden contar como dos caracteres)',
        '{count} de {limit} (emojis podem contar como dois caracteres)', '{count} sur {limit} (les emojis peuvent compter pour deux caractères)',
        '{count} / {limit} (emoji ikki belgi hisoblanishi mumkin)', '{count} з {limit} (эмодзі могуць займаць два сімвалы)'),
    'Не удалось загрузить доступные для изменения сведения.': (
        'Could not load editable details.', 'No se pudo cargar la información editable.',
        'Não foi possível carregar as informações editáveis.', 'Impossible de charger les informations modifiables.',
        'Tahrirlash mumkin bo‘lgan ma’lumotlarni yuklab bo‘lmadi.', 'Не ўдалося загрузіць даступныя для змены звесткі.'),
    'Изменение сведений недоступно для текущих прав или состояния чата.': (
        'Editing is unavailable with the current permissions or chat state.', 'La edición no está disponible con los permisos o el estado actual del chat.',
        'A edição está indisponível com as permissões ou o estado atual do chat.', 'La modification est indisponible avec les autorisations ou l’état actuel du chat.',
        'Joriy ruxsatlar yoki chat holatida ma’lumotlarni tahrirlab bo‘lmaydi.', 'Змена звестак недаступная з бягучымі правамі або станам чата.'),
    'Сведения обновлены.': ('Details updated.', 'Información actualizada.', 'Informações atualizadas.', 'Informations mises à jour.', 'Ma’lumotlar yangilandi.', 'Звесткі абноўлены.'),
    'Результат сохранения не подтверждён. Проверьте состояние; повторное сохранение остановлено.': (
        'Saving is not confirmed. Check the status; saving again is blocked.', 'El guardado no está confirmado. Comprueba el estado; volver a guardar está bloqueado.',
        'O salvamento não foi confirmado. Verifique o status; salvar novamente está bloqueado.', 'L’enregistrement n’est pas confirmé. Vérifiez l’état ; un nouvel enregistrement est bloqué.',
        'Saqlash tasdiqlanmadi. Holatni tekshiring; qayta saqlash bloklangan.', 'Захаванне не пацверджана. Праверце стан; паўторнае захаванне спынена.'),
    'Название подтверждено; описание ещё не подтверждено. Повторное сохранение остановлено.': (
        'The title is confirmed; the description is not yet confirmed. Saving again is blocked.', 'El nombre está confirmado; la descripción aún no. Volver a guardar está bloqueado.',
        'O nome foi confirmado; a descrição ainda não. Salvar novamente está bloqueado.', 'Le nom est confirmé ; la description ne l’est pas encore. Un nouvel enregistrement est bloqué.',
        'Nom tasdiqlandi; tavsif hali tasdiqlanmadi. Qayta saqlash bloklangan.', 'Назва пацверджана; апісанне яшчэ не пацверджана. Паўторнае захаванне спынена.'),
    'Описание подтверждено; название ещё не подтверждено. Повторное сохранение остановлено.': (
        'The description is confirmed; the title is not yet confirmed. Saving again is blocked.', 'La descripción está confirmada; el nombre aún no. Volver a guardar está bloqueado.',
        'A descrição foi confirmada; o nome ainda não. Salvar novamente está bloqueado.', 'La description est confirmée ; le nom ne l’est pas encore. Un nouvel enregistrement est bloqué.',
        'Tavsif tasdiqlandi; nom hali tasdiqlanmadi. Qayta saqlash bloklangan.', 'Апісанне пацверджана; назва яшчэ не пацверджана. Паўторнае захаванне спынена.'),
    'Изменения не отправлены. Откройте форму заново.': (
        'Changes were not sent. Open the form again.', 'Los cambios no se enviaron. Abre el formulario de nuevo.',
        'As alterações não foram enviadas. Abra o formulário novamente.', 'Les modifications n’ont pas été envoyées. Rouvrez le formulaire.',
        'O‘zgarishlar yuborilmadi. Shaklni qayta oching.', 'Змены не адпраўлены. Адкрыйце форму нанова.'),
    'MAX отклонил изменение сведений.': ('MAX rejected the changes.', 'MAX rechazó los cambios.', 'O MAX rejeitou as alterações.', 'MAX a refusé les modifications.', 'MAX o‘zgarishlarni rad etdi.', 'MAX адхіліў змены.'),
    'Проверка сохранения…': ('Checking saved changes…', 'Comprobando los cambios guardados…', 'Verificando alterações salvas…', 'Vérification des modifications enregistrées…', 'Saqlash tekshirilmoqda…', 'Праверка захавання…'),
    'Не удалось проверить сохранение. Показан последний известный результат.': (
        'Could not check saved changes. The last known result is shown.', 'No se pudieron comprobar los cambios guardados. Se muestra el último resultado conocido.',
        'Não foi possível verificar as alterações salvas. O último resultado conhecido é exibido.', 'Impossible de vérifier les modifications enregistrées. Le dernier résultat connu est affiché.',
        'Saqlashni tekshirib bo‘lmadi. Oxirgi ma’lum natija ko‘rsatilgan.', 'Не ўдалося праверыць захаванне. Паказаны апошні вядомы вынік.'),
    'Выберите нового владельца.': ('Choose a new owner.', 'Elige un nuevo propietario.', 'Escolha um novo proprietário.', 'Choisissez un nouveau propriétaire.', 'Yangi egani tanlang.', 'Выберыце новага ўладальніка.'),
    'Новый владелец': ('New owner', 'Nuevo propietario', 'Novo proprietário', 'Nouveau propriétaire', 'Yangi ega', 'Новы ўладальнік'),
    'новому владельцу': ('the new owner', 'el nuevo propietario', 'o novo proprietário', 'le nouveau propriétaire', 'yangi egaga', 'новаму ўладальніку'),
    'Администратор': ('Admin', 'Administrador', 'Administrador', 'Administrateur', 'Administrator', 'Адміністратар'),
    'Участник': ('Member', 'Miembro', 'Membro', 'Membre', 'Ishtirokchi', 'Удзельнік'),
    'Владелец': ('Owner', 'Propietario', 'Proprietário', 'Propriétaire', 'Ega', 'Уладальнік'),
    'Показать ещё': ('Show more', 'Mostrar más', 'Mostrar mais', 'Afficher plus', 'Yana ko‘rsatish', 'Паказаць яшчэ'),
    'Загрузка участников…': ('Loading members…', 'Cargando miembros…', 'Carregando membros…', 'Chargement des membres…', 'Ishtirokchilar yuklanmoqda…', 'Загрузка ўдзельнікаў…'),
    'Нажмите «Найти», чтобы загрузить участников.': (
        'Click Search to load members.', 'Pulsa Buscar para cargar los miembros.',
        'Clique em Buscar para carregar os membros.', 'Cliquez sur Rechercher pour charger les membres.',
        'Ishtirokchilarni yuklash uchun «Qidirish»ni bosing.', 'Націсніце «Знайсці», каб загрузіць удзельнікаў.'),
    'Не удалось загрузить участников. Повторите поиск.': (
        'Could not load members. Search again.', 'No se pudieron cargar los miembros. Repite la búsqueda.',
        'Não foi possível carregar os membros. Pesquise novamente.', 'Impossible de charger les membres. Relancez la recherche.',
        'Ishtirokchilarni yuklab bo‘lmadi. Qayta qidiring.', 'Не ўдалося загрузіць удзельнікаў. Паўтарыце пошук.'),
    'Показана часть списка. Можно уточнить имя.': (
        'Part of the list is shown. You can refine the name.', 'Se muestra parte de la lista. Puedes precisar el nombre.',
        'Parte da lista é exibida. Você pode refinar o nome.', 'Une partie de la liste est affichée. Vous pouvez préciser le nom.',
        'Ro‘yxatning bir qismi ko‘rsatilgan. Ismni aniqlashtirish mumkin.', 'Паказана частка спіса. Можна ўдакладніць імя.'),
    'Подходящие участники не найдены.': (
        'No eligible members found.', 'No se encontraron miembros elegibles.', 'Nenhum membro elegível encontrado.',
        'Aucun membre admissible trouvé.', 'Mos ishtirokchilar topilmadi.', 'Прыдатныя ўдзельнікі не знойдзены.'),
    'Одноимённые записи без различимых данных недоступны для выбора.': (
        'Entries with identical names and no distinguishing details cannot be selected.',
        'No se pueden seleccionar entradas con nombres idénticos sin datos que las distingan.',
        'Entradas com nomes idênticos e sem dados que as diferenciem não podem ser selecionadas.',
        'Les entrées homonymes sans détail distinctif ne peuvent pas être sélectionnées.',
        'Farqlovchi ma’lumotlarsiz bir xil ismli yozuvlarni tanlab bo‘lmaydi.',
        'Аднайменныя запісы без адрозных звестак недаступныя для выбару.'),
    'Этот участник станет владельцем. Вы потеряете права владельца, затем выйдете. Передача прав не отменяется при сбое выхода.': (
        'This member will become the owner. You will give up ownership and then leave. A failed departure does not undo the transfer.',
        'Este miembro será el propietario. Cederás la propiedad y después saldrás. Un fallo al salir no anula la transferencia.',
        'Este membro se tornará o proprietário. Você transferirá a propriedade e depois sairá. Uma falha ao sair não desfaz a transferência.',
        'Ce membre deviendra propriétaire. Vous céderez la propriété puis quitterez la conversation. Un échec de sortie n’annule pas le transfert.',
        'Bu ishtirokchi egaga aylanadi. Egalik huquqini topshirib, keyin chiqasiz. Chiqishdagi xato huquq o‘tkazilishini bekor qilmaydi.',
        'Гэты ўдзельнік стане ўладальнікам. Вы страціце правы ўладальніка, затым выйдзеце. Збой выхаду не адмяняе перадачу правоў.'),
    'Права уже переданы этому участнику. Сейчас будет выполнен только выход.': (
        'Ownership has already been transferred to this member. Only leaving will be performed now.',
        'La propiedad ya se transfirió a este miembro. Ahora solo se realizará la salida.',
        'A propriedade já foi transferida a este membro. Agora será realizada apenas a saída.',
        'La propriété a déjà été transférée à ce membre. Seule la sortie sera effectuée maintenant.',
        'Huquqlar bu ishtirokchiga allaqachon o‘tkazilgan. Endi faqat chiqish bajariladi.',
        'Правы ўжо перададзены гэтаму ўдзельніку. Цяпер будзе выкананы толькі выхад.'),
    'Права переданы: {name}. Вы вышли из чата.': (
        'Ownership transferred to {name}. You left the chat.', 'Propiedad transferida a {name}. Has salido del chat.',
        'Propriedade transferida para {name}. Você saiu do chat.', 'Propriété transférée à {name}. Vous avez quitté la conversation.',
        'Huquqlar {name} ga o‘tkazildi. Chatdan chiqdingiz.', 'Правы перададзены: {name}. Вы выйшлі з чата.'),
    'Права переданы: {name}. Выход не подтверждён.': (
        'Ownership transferred to {name}. Leaving is not confirmed.', 'Propiedad transferida a {name}. No se ha confirmado la salida.',
        'Propriedade transferida para {name}. A saída não foi confirmada.', 'Propriété transférée à {name}. La sortie n’est pas confirmée.',
        'Huquqlar {name} ga o‘tkazildi. Chiqish tasdiqlanmadi.', 'Правы перададзены: {name}. Выхад не пацверджаны.'),
    'Передача прав не подтверждена. Выход не запускался. Проверьте состояние.': (
        'The transfer is unconfirmed. Leaving has not started. Check status.',
        'La transferencia no está confirmada. La salida no se ha iniciado. Comprueba el estado.',
        'A transferência não foi confirmada. A saída não foi iniciada. Verifique o status.',
        'Le transfert n’est pas confirmé. La sortie n’a pas commencé. Vérifiez l’état.',
        'Huquqlar o‘tkazilgani tasdiqlanmadi. Chiqish boshlanmadi. Holatni tekshiring.',
        'Перадача правоў не пацверджана. Выхад не пачынаўся. Праверце стан.'),
    'Результат не подтверждён. Проверьте состояние перед новым действием.': (
        'The result is unconfirmed. Check status before another action.',
        'El resultado no está confirmado. Comprueba el estado antes de otra acción.',
        'O resultado não foi confirmado. Verifique o status antes de outra ação.',
        'Le résultat n’est pas confirmé. Vérifiez l’état avant une nouvelle action.',
        'Natija tasdiqlanmadi. Yangi amaldan oldin holatni tekshiring.',
        'Вынік не пацверджаны. Праверце стан перад новым дзеяннем.'),
    'Бот остановлен. Чат не удалён. Обновите сведения перед продолжением.': (
        'The bot is stopped. The chat was not deleted. Refresh its details before continuing.',
        'El bot está detenido. El chat no se ha eliminado. Actualiza la información antes de continuar.',
        'O bot foi parado. O chat não foi excluído. Atualize os dados antes de continuar.',
        'Le bot est arrêté. La conversation n’a pas été supprimée. Actualisez les informations avant de continuer.',
        'Bot to‘xtatildi. Chat o‘chirilmadi. Davom etishdan oldin ma’lumotlarni yangilang.',
        'Бот спынены. Чат не выдалены. Абнавіце звесткі перад працягам.'),
    'Бот остановлен. Чат удалён.': (
        'The bot is stopped. The chat is deleted.', 'El bot está detenido. El chat se ha eliminado.',
        'O bot foi parado. O chat foi excluído.', 'Le bot est arrêté. La conversation est supprimée.',
        'Bot to‘xtatildi. Chat o‘chirildi.', 'Бот спынены. Чат выдалены.'),
    'Бот остановлен. Чат не удалён. Удаление можно продолжить отдельно.': (
        'The bot is stopped. The chat was not deleted. You can continue deletion separately.',
        'El bot está detenido. El chat no se ha eliminado. Puedes continuar la eliminación por separado.',
        'O bot foi parado. O chat não foi excluído. Você pode continuar a exclusão separadamente.',
        'Le bot est arrêté. La conversation n’a pas été supprimée. Vous pouvez poursuivre la suppression séparément.',
        'Bot to‘xtatildi. Chat o‘chirilmadi. O‘chirishni alohida davom ettirish mumkin.',
        'Бот спынены. Чат не выдалены. Выдаленне можна працягнуць асобна.'),
    'Бот остановлен. Удаление чата не подтверждено. Проверка состояния не повторяет удаление.': (
        'The bot is stopped. Chat deletion is unconfirmed. Checking status does not repeat deletion.',
        'El bot está detenido. La eliminación del chat no está confirmada. Comprobar el estado no repite la eliminación.',
        'O bot foi parado. A exclusão do chat não foi confirmada. Verificar o status não repete a exclusão.',
        'Le bot est arrêté. La suppression n’est pas confirmée. Vérifier l’état ne répète pas la suppression.',
        'Bot to‘xtatildi. Chat o‘chirilgani tasdiqlanmadi. Holatni tekshirish o‘chirishni takrorlamaydi.',
        'Бот спынены. Выдаленне чата не пацверджана. Праверка стану не паўтарае выдаленне.'),
    'Остановка бота не подтверждена. Удаление чата не запускалось. Проверьте состояние.': (
        'Stopping the bot is unconfirmed. Chat deletion has not started. Check status.',
        'No se ha confirmado la detención del bot. La eliminación del chat no se ha iniciado. Comprueba el estado.',
        'A parada do bot não foi confirmada. A exclusão do chat não foi iniciada. Verifique o status.',
        'L’arrêt du bot n’est pas confirmé. La suppression n’a pas commencé. Vérifiez l’état.',
        'Bot to‘xtatilgani tasdiqlanmadi. Chatni o‘chirish boshlanmadi. Holatni tekshiring.',
        'Спыненне бота не пацверджана. Выдаленне чата не пачыналася. Праверце стан.'),
    'Бот уже остановлен. Будет выполнено только удаление чата у вас.': (
        'The bot is already stopped. Only your copy of the chat will be deleted.',
        'El bot ya está detenido. Solo se eliminará tu copia del chat.',
        'O bot já foi parado. Apenas a sua cópia do chat será excluída.',
        'Le bot est déjà arrêté. Seule votre copie de la conversation sera supprimée.',
        'Bot allaqachon to‘xtatilgan. Faqat sizdagi chat o‘chiriladi.',
        'Бот ужо спынены. Будзе выдалены толькі чат у вас.'),
    'Сначала приложение подтвердит остановку бота, затем удалит чат только у вас. Если удаление не завершится, бот останется остановленным.': (
        'The app will first confirm that the bot is stopped, then delete only your copy of the chat. If deletion does not finish, the bot will remain stopped.',
        'La aplicación confirmará primero que el bot está detenido y después eliminará solo tu copia del chat. Si la eliminación no se completa, el bot seguirá detenido.',
        'O aplicativo primeiro confirmará que o bot foi parado e depois excluirá apenas a sua cópia do chat. Se a exclusão não terminar, o bot continuará parado.',
        'L’application confirmera d’abord l’arrêt du bot, puis supprimera uniquement votre copie de la conversation. Si la suppression échoue, le bot restera arrêté.',
        'Ilova avval bot to‘xtatilganini tasdiqlaydi, keyin faqat sizdagi chatni o‘chiradi. O‘chirish tugamasa, bot to‘xtatilgan holda qoladi.',
        'Праграма спачатку пацвердзіць спыненне бота, потым выдаліць чат толькі ў вас. Калі выдаленне не завершыцца, бот застанецца спыненым.'),
    'Убрать из локального списка': (
        'Remove from local list', 'Quitar de la lista local', 'Remover da lista local',
        'Retirer de la liste locale', 'Mahalliy ro‘yxatdan olib tashlash', 'Прыбраць з лакальнага спіса'),
    'Убрать «{name}» из локального списка этого приложения? Переписка сохранится.': (
        'Remove “{name}” from this app’s local list? The conversation will be kept.',
        '¿Quitar a «{name}» de la lista local de esta aplicación? La conversación se conservará.',
        'Remover «{name}» da lista local deste aplicativo? A conversa será mantida.',
        'Retirer « {name} » de la liste locale de cette application ? La conversation sera conservée.',
        '«{name}» ushbu ilovaning mahalliy ro‘yxatidan olib tashlansinmi? Yozishma saqlanadi.',
        'Прыбраць «{name}» з лакальнага спіса гэтай праграмы? Перапіска захаваецца.'),
    'Контакт убран из локального списка. Переписка сохранена.': (
        'Contact removed from the local list. The conversation was kept.',
        'Contacto quitado de la lista local. La conversación se ha conservado.',
        'Contato removido da lista local. A conversa foi mantida.',
        'Contact retiré de la liste locale. La conversation a été conservée.',
        'Kontakt mahalliy ro‘yxatdan olib tashlandi. Yozishma saqlandi.',
        'Кантакт прыбраны з лакальнага спіса. Перапіска захаваная.'),
    'Не удалось убрать контакт из локального списка. Попробуйте ещё раз.': (
        'Could not remove the contact from the local list. Try again.',
        'No se pudo quitar el contacto de la lista local. Inténtalo de nuevo.',
        'Não foi possível remover o contato da lista local. Tente novamente.',
        'Impossible de retirer le contact de la liste locale. Réessayez.',
        'Kontaktni mahalliy ro‘yxatdan olib tashlab bo‘lmadi. Qayta urinib ko‘ring.',
        'Не ўдалося прыбраць кантакт з лакальнага спіса. Паспрабуйце яшчэ раз.'),
    'Пожаловаться на бота?': (
        'Report this bot?', '¿Denunciar al bot?', 'Denunciar o bot?',
        'Signaler le bot ?', 'Bot ustidan shikoyat qilinsinmi?', 'Паскардзіцца на бота?'),
    'Загрузка причин жалобы…': (
        'Loading report reasons…', 'Cargando los motivos de denuncia…', 'Carregando motivos da denúncia…',
        'Chargement des motifs du signalement…', 'Shikoyat sabablari yuklanmoqda…', 'Загрузка прычын скаргі…'),
    'Не удалось загрузить причины жалобы.': (
        'Could not load report reasons.', 'No se pudieron cargar los motivos de denuncia.',
        'Não foi possível carregar os motivos da denúncia.', 'Impossible de charger les motifs du signalement.',
        'Shikoyat sabablarini yuklab bo‘lmadi.', 'Не ўдалося загрузіць прычыны скаргі.'),
    'Нет доступных причин жалобы.': (
        'No report reasons are available.', 'No hay motivos de denuncia disponibles.',
        'Não há motivos de denúncia disponíveis.', 'Aucun motif de signalement n’est disponible.',
        'Shikoyat sabablari mavjud emas.', 'Няма даступных прычын скаргі.'),
    'Жалоба недоступна для текущих прав или состояния чата.': (
        'Reporting is unavailable with the current permissions or chat state.',
        'No se puede denunciar con los permisos o el estado actuales del chat.',
        'Não é possível denunciar com as permissões ou o estado atuais do chat.',
        'Le signalement est indisponible avec les autorisations ou l’état actuels du chat.',
        'Joriy ruxsatlar yoki chat holati bilan shikoyat yuborish imkonsiz.',
        'Скарга недаступная з бягучымі правамі або станам чата.'),
    'Укажите причину жалобы': (
        'Select a report reason', 'Selecciona un motivo de denuncia', 'Selecione o motivo da denúncia',
        'Choisissez un motif de signalement', 'Shikoyat sababini tanlang', 'Выберыце прычыну скаргі'),
    'Отправка жалобы…': (
        'Sending report…', 'Enviando la denuncia…', 'Enviando a denúncia…',
        'Envoi du signalement…', 'Shikoyat yuborilmoqda…', 'Адпраўка скаргі…'),
    'Жалоба отправлена': (
        'Report sent', 'Denuncia enviada', 'Denúncia enviada',
        'Signalement envoyé', 'Shikoyat yuborildi', 'Скарга адпраўлена'),
    'Жалоба на этот чат уже отправлена.': (
        'A report for this chat has already been sent.', 'Ya se ha enviado una denuncia sobre este chat.',
        'Já foi enviada uma denúncia sobre este chat.', 'Un signalement concernant ce chat a déjà été envoyé.',
        'Bu chat ustidan shikoyat allaqachon yuborilgan.', 'Скарга на гэты чат ужо адпраўлена.'),
    'Жалоба не отправлена. Откройте окно заново и проверьте причину.': (
        'The report was not sent. Reopen the window and check the reason.',
        'La denuncia no se ha enviado. Abre de nuevo la ventana y comprueba el motivo.',
        'A denúncia não foi enviada. Abra a janela novamente e confira o motivo.',
        'Le signalement n’a pas été envoyé. Rouvrez la fenêtre et vérifiez le motif.',
        'Shikoyat yuborilmadi. Oynani qayta oching va sababni tekshiring.',
        'Скарга не адпраўлена. Адкрыйце акно зноў і праверце прычыну.'),
    'MAX отклонил жалобу.': (
        'MAX rejected the report.', 'MAX ha rechazado la denuncia.', 'O MAX rejeitou a denúncia.',
        'MAX a rejeté le signalement.', 'MAX shikoyatni rad etdi.', 'MAX адхіліў скаргу.'),
    'Результат отправки не подтверждён. Повторная отправка остановлена.': (
        'Submission has not been confirmed. Resubmission is blocked.',
        'No se ha confirmado el envío. Se ha bloqueado el reenvío.',
        'O envio não foi confirmado. O reenvio está bloqueado.',
        'L’envoi n’est pas confirmé. Tout nouvel envoi est bloqué.',
        'Yuborish natijasi tasdiqlanmadi. Qayta yuborish to‘xtatildi.',
        'Вынік адпраўкі не пацверджаны. Паўторная адпраўка спынена.'),
    'Вы вышли из чата.': (
        'You left the chat.', 'Has salido del chat.', 'Você saiu do chat.',
        'Vous avez quitté le chat.', 'Siz chatdan chiqdingiz.', 'Вы выйшлі з чата.'),
    'Предпросмотр': ('Preview', 'Vista previa', 'Prévia', 'Aperçu', 'Oldindan ko‘rish', 'Перадпрагляд'),
    'Без отметки о прочтении': ('Without marking as read', 'Sin marcar como leído', 'Sem marcar como lido', 'Sans marquer comme lu', 'O‘qilgan deb belgilamasdan', 'Без пазнакі пра прачытанне'),
    'Не удалось загрузить предпросмотр.': ('Could not load the preview.', 'No se pudo cargar la vista previa.', 'Não foi possível carregar a prévia.', 'Impossible de charger l’aperçu.', 'Oldindan ko‘rishni yuklab bo‘lmadi.', 'Не ўдалося загрузіць перадпрагляд.'),
    'Отметка о прочтении': ('Read status', 'Estado de lectura', 'Status de leitura', 'État de lecture', 'O‘qilganlik holati', 'Стан прачытання'),
    'Закрепление чата': ('Chat pinning', 'Fijar chat', 'Fixar chat', 'Épinglage du chat', 'Chatni mahkamlash', 'Замацаванне чата'),
    'На 24 часа': ('For 24 hours', 'Durante 24 horas', 'Por 24 horas', 'Pendant 24 heures', '24 soatga', 'На 24 гадзіны'),
    'Отписаться от канала': ('Unsubscribe from channel', 'Dejar de seguir el canal', 'Deixar de seguir o canal', 'Se désabonner du canal', 'Kanal obunasini bekor qilish', 'Адпісацца ад канала'),
    'Выйти из чата': ('Leave chat', 'Salir del chat', 'Sair do chat', 'Quitter le chat', 'Chatdan chiqish', 'Выйсці з чата'),
    'Действие пока недоступно': ('Action not available yet', 'Acción aún no disponible', 'Ação ainda indisponível', 'Action pas encore disponible', 'Amal hozircha mavjud emas', 'Дзеянне пакуль недаступнае'),
    'Загрузка папок…': ('Loading folders…', 'Cargando carpetas…', 'Carregando pastas…', 'Chargement des dossiers…', 'Papkalar yuklanmoqda…', 'Загрузка папак…'),
    'Обновить папки': ('Refresh folders', 'Actualizar carpetas', 'Atualizar pastas', 'Actualiser les dossiers', 'Papkalarni yangilash', 'Абнавіць папкі'),
    'Проверить состояние': ('Check status', 'Comprobar estado', 'Verificar status', 'Vérifier l’état', 'Holatni tekshirish', 'Праверыць стан'),
    'Не удалось подтвердить действие. Проверьте состояние.': (
        'Could not confirm the action. Check the status.',
        'No se pudo confirmar la acción. Comprueba el estado.',
        'Não foi possível confirmar a ação. Verifique o status.',
        'Impossible de confirmer l’action. Vérifiez l’état.',
        'Amalni tasdiqlab bo‘lmadi. Holatni tekshiring.',
        'Не ўдалося пацвердзіць дзеянне. Праверце стан.'),
    'Вы скопировали ссылку': ('Link copied', 'Enlace copiado', 'Link copiado', 'Lien copié', 'Havola nusxalandi', 'Спасылка скапіявана'),
    'Не удалось скопировать приглашение. Проверьте соединение с MAX и повторите действие.': (
        'Could not copy the invitation. Check your MAX connection and try again.',
        'No se pudo copiar la invitación. Comprueba tu conexión con MAX e inténtalo de nuevo.',
        'Não foi possível copiar o convite. Verifique sua conexão com o MAX e tente novamente.',
        'Impossible de copier l’invitation. Vérifiez votre connexion à MAX et réessayez.',
        'Taklifni nusxalab bo‘lmadi. MAX bilan ulanishni tekshiring va qayta urinib ko‘ring.',
        'Не ўдалося скапіяваць запрашэнне. Праверце злучэнне з MAX і паўтарыце дзеянне.'),
    # Menu availability and the explicit release of an unconfirmed creation journal.
    'Не удалось скопировать приглашение: MAX не передал текст приглашения для этой сессии.': (
        'Could not copy the invitation: MAX has not provided invitation text for this session.',
        'No se pudo copiar la invitación: MAX no ha proporcionado el texto de invitación para esta sesión.',
        'Não foi possível copiar o convite: o MAX não forneceu o texto do convite para esta sessão.',
        'Impossible de copier l’invitation : MAX n’a pas fourni le texte d’invitation pour cette session.',
        'Taklifni nusxalab bo‘lmadi: MAX bu seans uchun taklif matnini bermadi.',
        'Не ўдалося скапіяваць запрашэнне: MAX не перадаў тэкст запрашэння для гэтага сеанса.'),
    'Нужна подтверждённая WEB-сессия MAX. Дождитесь подключения и откройте меню снова.': (
        'A confirmed MAX web session is required. Wait for the connection and open the menu again.',
        'Se necesita una sesión web de MAX confirmada. Espera a la conexión y vuelve a abrir el menú.',
        'É necessária uma sessão web do MAX confirmada. Aguarde a conexão e abra o menu novamente.',
        'Une session web MAX confirmée est requise. Attendez la connexion et rouvrez le menu.',
        'Tasdiqlangan MAX veb-seansi kerak. Ulanishni kuting va menyuni qayta oching.',
        'Патрэбны пацверджаны вэб-сеанс MAX. Дачакайцеся падключэння і адкрыйце меню зноў.'),
    'Снять блокировку…': ('Remove lock…', 'Quitar bloqueo…', 'Remover bloqueio…', 'Lever le blocage…',
        'Blokni olib tashlash…', 'Зняць блакіроўку…'),
    'Снять блокировку': ('Remove lock', 'Quitar bloqueo', 'Remover bloqueio', 'Lever le blocage',
        'Blokni olib tashlash', 'Зняць блакіроўку'),
    'Снять блокировку создания': ('Remove the creation lock', 'Quitar el bloqueo de creación',
        'Remover o bloqueio de criação', 'Lever le blocage de création', 'Yaratish blokini olib tashlash',
        'Зняць блакіроўку стварэння'),
    'MAX не подтвердил предыдущее создание «{title}». Возможно, чат уже создан: сначала проверьте список чатов. Снять блокировку? Повторный запрос не отправляется.': (
        'MAX did not confirm the previous creation of “{title}”. The chat may already exist: check your chat list first. Remove the lock? No request is resent.',
        'MAX no confirmó la creación anterior de «{title}». Es posible que el chat ya exista: revisa primero la lista de chats. ¿Quitar el bloqueo? No se reenvía ninguna solicitud.',
        'O MAX não confirmou a criação anterior de “{title}”. O chat pode já existir: verifique primeiro a lista de chats. Remover o bloqueio? Nenhuma solicitação é reenviada.',
        'MAX n’a pas confirmé la création précédente de « {title} ». La discussion existe peut-être déjà : vérifiez d’abord la liste des chats. Lever le blocage ? Aucune requête n’est renvoyée.',
        'MAX «{title}» ning avvalgi yaratilishini tasdiqlamadi. Chat allaqachon yaratilgan bo‘lishi mumkin: avval chatlar ro‘yxatini tekshiring. Blok olib tashlansinmi? So‘rov qayta yuborilmaydi.',
        'MAX не пацвердзіў папярэдняе стварэнне «{title}». Магчыма, чат ужо створаны: спачатку праверце спіс чатаў. Зняць блакіроўку? Паўторны запыт не адпраўляецца.'),
    'Не удалось снять блокировку. Откройте создание заново.': (
        'Could not remove the lock. Open creation again.',
        'No se pudo quitar el bloqueo. Vuelve a abrir la creación.',
        'Não foi possível remover o bloqueio. Abra a criação novamente.',
        'Impossible de lever le blocage. Rouvrez la création.',
        'Blokni olib tashlab bo‘lmadi. Yaratishni qayta oching.',
        'Не ўдалося зняць блакіроўку. Адкрыйце стварэнне зноў.'),
    'Блокировка снята. Повторный запрос не отправлялся; можно создать новый чат.': (
        'The lock was removed. No request was resent; you can create a new chat.',
        'Se quitó el bloqueo. No se reenvió ninguna solicitud; puedes crear un chat nuevo.',
        'O bloqueio foi removido. Nenhuma solicitação foi reenviada; você pode criar um novo chat.',
        'Le blocage a été levé. Aucune requête n’a été renvoyée ; vous pouvez créer une nouvelle discussion.',
        'Blok olib tashlandi. So‘rov qayta yuborilmadi; yangi chat yaratishingiz mumkin.',
        'Блакіроўка знята. Паўторны запыт не адпраўляўся; можна стварыць новы чат.'),
    'Русский': ('Russian', 'Ruso', 'Russo', 'Russe', 'Ruscha', 'Руская'),
    'Английский': ('English', 'Inglés', 'Inglês', 'Anglais', 'Inglizcha', 'Англійская'),
    'Испанский': ('Spanish', 'Español', 'Espanhol', 'Espagnol', 'Ispancha', 'Іспанская'),
    'Португальский (Бразилия)': ('Portuguese (Brazil)', 'Portugués (Brasil)', 'Português (Brasil)', 'Portugais (Brésil)', 'Portugalcha (Braziliya)', 'Партугальская (Бразілія)'),
    'Французский': ('French', 'Francés', 'Francês', 'Français', 'Fransuzcha', 'Французская'),
    'Узбекский': ('Uzbek', 'Uzbeko', 'Uzbeque', 'Ouzbek', 'O‘zbekcha', 'Узбекская'),
    'Белорусский': ('Belarusian', 'Bielorruso', 'Bielorrusso', 'Biélorusse', 'Belaruscha', 'Беларуская'),
    'Некоторые элементы интерфейса пока остаются на русском языке.': ('Some interface elements are still in Russian.', 'Algunos elementos de la interfaz siguen en ruso.', 'Alguns elementos da interface ainda estão em russo.', 'Certains éléments de l’interface sont encore en russe.', 'Interfeysning ayrim qismlari hozircha rus tilida.', 'Некаторыя элементы інтэрфейсу пакуль застаюцца на рускай мове.'),
    'Не удалось сохранить язык. Попробуйте ещё раз.': ('Could not save the language. Try again.', 'No se pudo guardar el idioma. Inténtalo de nuevo.', 'Não foi possível salvar o idioma. Tente novamente.', 'Impossible d’enregistrer la langue. Réessayez.', 'Tilni saqlab bo‘lmadi. Qayta urinib ko‘ring.', 'Не ўдалося захаваць мову. Паспрабуйце яшчэ раз.'),
    'Данные и память': ('Data and storage', 'Datos y almacenamiento', 'Dados e armazenamento', 'Données et stockage', 'Ma’lumotlar va xotira', 'Даныя і памяць'),
    'Сочетания клавиш': ('Keyboard shortcuts', 'Atajos de teclado', 'Atalhos de teclado', 'Raccourcis clavier', 'Klaviatura yorliqlari', 'Спалучэнні клавіш'),
    'Неизвестно': ('Unknown', 'Desconocido', 'Desconhecido', 'Inconnu', 'Noma’lum', 'Невядома'),
    'Состояние неизвестно': ('Status unknown', 'Estado desconocido', 'Estado desconhecido', 'État inconnu', 'Holat noma’lum', 'Стан невядомы'),
    'Состояние пароля неизвестно.': ('Password status is unknown.', 'Se desconoce el estado de la contraseña.', 'O estado da senha é desconhecido.', 'L’état du mot de passe est inconnu.', 'Parol holati noma’lum.', 'Стан пароля невядомы.'),
    'Загрузка…': ('Loading…', 'Cargando…', 'Carregando…', 'Chargement…', 'Yuklanmoqda…', 'Загрузка…'),
    'Сохранение…': ('Saving…', 'Guardando…', 'Salvando…', 'Enregistrement…', 'Saqlanmoqda…', 'Захаванне…'),
    'Сохранить изменения': ('Save changes', 'Guardar cambios', 'Salvar alterações', 'Enregistrer les modifications', 'O‘zgarishlarni saqlash', 'Захаваць змены'),
    'Мой профиль': ('My profile', 'Mi perfil', 'Meu perfil', 'Mon profil', 'Mening profilim', 'Мой профіль'),
    'Фото профиля': ('Profile photo', 'Foto de perfil', 'Foto de perfil', 'Photo de profil', 'Profil rasmi', 'Фота профілю'),
    'Посмотреть фото профиля': ('View profile photo', 'Ver foto de perfil', 'Ver foto de perfil', 'Voir la photo de profil', 'Profil rasmini ko‘rish', 'Паглядзець фота профілю'),
    'Загрузить фото профиля': ('Upload profile photo', 'Subir foto de perfil', 'Carregar foto de perfil', 'Importer une photo de profil', 'Profil rasmini yuklash', 'Загрузіць фота профілю'),
    'Загрузка фото профиля…': ('Uploading profile photo…', 'Subiendo foto de perfil…', 'Carregando foto de perfil…', 'Importation de la photo de profil…', 'Profil rasmi yuklanmoqda…', 'Загрузка фота профілю…'),
    'Дополнительные действия профиля': ('More profile actions', 'Más acciones del perfil', 'Mais ações do perfil', 'Autres actions du profil', 'Profilning qo‘shimcha amallari', 'Дадатковыя дзеянні з профілем'),
    'Копировать номер телефона': ('Copy phone number', 'Copiar número de teléfono', 'Copiar número de telefone', 'Copier le numéro de téléphone', 'Telefon raqamini nusxalash', 'Скапіяваць нумар тэлефона'),
    'Копировать ID профиля': ('Copy profile ID', 'Copiar ID del perfil', 'Copiar ID do perfil', 'Copier l’identifiant du profil', 'Profil ID sini nusxalash', 'Скапіяваць ID профілю'),
    'Введите имя.': ('Enter a first name.', 'Introduce un nombre.', 'Digite um nome.', 'Saisissez un prénom.', 'Ismni kiriting.', 'Увядзіце імя.'),
    'Не более 59 символов': ('Up to 59 characters', 'Hasta 59 caracteres', 'Até 59 caracteres', '59 caractères maximum', 'Ko‘pi bilan 59 ta belgi', 'Не больш за 59 сімвалаў'),
    'Включить пароль': ('Enable password', 'Activar contraseña', 'Ativar senha', 'Activer le mot de passe', 'Parolni yoqish', 'Уключыць пароль'),
    'Изменение подтверждается текущим паролем': ('Confirm the change with the current password', 'Confirma el cambio con la contraseña actual', 'Confirme a alteração com a senha atual', 'Confirmez avec le mot de passe actuel', 'O‘zgarish joriy parol bilan tasdiqlanadi', 'Змена пацвярджаецца бягучым паролем'),
    'Почта для восстановления': ('Recovery email', 'Correo de recuperación', 'E-mail de recuperação', 'Adresse e-mail de récupération', 'Tiklash uchun elektron pochta', 'Пошта для аднаўлення'),
    'Пароль для входа включён.': ('Sign-in password is enabled.', 'La contraseña de acceso está activada.', 'A senha de acesso está ativada.', 'Le mot de passe de connexion est activé.', 'Kirish paroli yoqilgan.', 'Пароль для ўваходу ўключаны.'),
    'Пароль для входа отключён.': ('Sign-in password is disabled.', 'La contraseña de acceso está desactivada.', 'A senha de acesso está desativada.', 'Le mot de passe de connexion est désactivé.', 'Kirish paroli o‘chirilgan.', 'Пароль для ўваходу адключаны.'),
    'Обновить статус пароля': ('Refresh password status', 'Actualizar estado de la contraseña', 'Atualizar estado da senha', 'Actualiser l’état du mot de passe', 'Parol holatini yangilash', 'Абнавіць стан пароля'),
    'Доступно только в мобильном приложении': ('Available only in the mobile app', 'Disponible solo en la aplicación móvil', 'Disponível apenas no aplicativo móvel', 'Disponible uniquement dans l’application mobile', 'Faqat mobil ilovada mavjud', 'Даступна толькі ў мабільнай праграме'),
    'Обновить настройки': ('Refresh settings', 'Actualizar ajustes', 'Atualizar configurações', 'Actualiser les paramètres', 'Sozlamalarni yangilash', 'Абнавіць налады'),
    'Загрузка настроек…': ('Loading settings…', 'Cargando ajustes…', 'Carregando configurações…', 'Chargement des paramètres…', 'Sozlamalar yuklanmoqda…', 'Загрузка налад…'),
    'Заблокированные контакты': ('Blocked contacts', 'Contactos bloqueados', 'Contatos bloqueados', 'Contacts bloqués', 'Bloklangan kontaktlar', 'Заблакаваныя кантакты'),
    'Активные устройства': ('Active devices', 'Dispositivos activos', 'Dispositivos ativos', 'Appareils actifs', 'Faol qurilmalar', 'Актыўныя прылады'),
    'Текущее устройство': ('Current device', 'Dispositivo actual', 'Dispositivo atual', 'Appareil actuel', 'Joriy qurilma', 'Бягучая прылада'),
    'Текущая': ('Current', 'Actual', 'Atual', 'Actuelle', 'Joriy', 'Бягучая'),
    'Почта: ': ('Email: ', 'Correo: ', 'E-mail: ', 'E-mail : ', 'Pochta: ', 'Пошта: '),
    'Устройство': ('Device', 'Dispositivo', 'Dispositivo', 'Appareil', 'Qurilma', 'Прылада'),
    'Обновить устройства': ('Refresh devices', 'Actualizar dispositivos', 'Atualizar dispositivos', 'Actualiser les appareils', 'Qurilmalarni yangilash', 'Абнавіць прылады'),
    'Загрузка устройств…': ('Loading devices…', 'Cargando dispositivos…', 'Carregando dispositivos…', 'Chargement des appareils…', 'Qurilmalar yuklanmoqda…', 'Загрузка прылад…'),
    'Устройства ещё не загружены.': ('Devices have not been loaded yet.', 'Los dispositivos aún no se han cargado.', 'Os dispositivos ainda não foram carregados.', 'Les appareils ne sont pas encore chargés.', 'Qurilmalar hali yuklanmagan.', 'Прылады яшчэ не загружаны.'),
    'Сервер не вернул данные об устройствах.': ('The server returned no device information.', 'El servidor no devolvió información de dispositivos.', 'O servidor não retornou dados dos dispositivos.', 'Le serveur n’a renvoyé aucune information sur les appareils.', 'Server qurilmalar haqida ma’lumot qaytarmadi.', 'Сервер не вярнуў звесткі пра прылады.'),
    'Дополнительно: цвет акцента': ('Additional: accent color', 'Adicional: color de acento', 'Adicional: cor de destaque', 'Autre : couleur d’accentuation', 'Qo‘shimcha: aksent rangi', 'Дадаткова: колер акцэнту'),
    'Размер текста в чате': ('Chat text size', 'Tamaño del texto del chat', 'Tamanho do texto do chat', 'Taille du texte des chats', 'Chatdagi matn o‘lchami', 'Памер тэксту ў чаце'),
    'Темы оформления — прокручиваемая лента': ('Themes — scrollable list', 'Temas — lista desplazable', 'Temas — lista rolável', 'Thèmes — liste défilante', 'Mavzular — aylantiriladigan ro‘yxat', 'Тэмы афармлення — спіс з пракруткай'),
    'Папка загрузок': ('Downloads folder', 'Carpeta de descargas', 'Pasta de downloads', 'Dossier de téléchargement', 'Yuklab olish papkasi', 'Папка загрузак'),
    'Изменить папку': ('Change folder', 'Cambiar carpeta', 'Alterar pasta', 'Changer de dossier', 'Papkani o‘zgartirish', 'Змяніць папку'),
    'Открыть папку загрузок': ('Open downloads folder', 'Abrir carpeta de descargas', 'Abrir pasta de downloads', 'Ouvrir le dossier de téléchargement', 'Yuklab olish papkasini ochish', 'Адкрыць папку загрузак'),
    'Выберите существующую папку загрузок.': ('Choose an existing downloads folder.', 'Elige una carpeta de descargas existente.', 'Escolha uma pasta de downloads existente.', 'Choisissez un dossier de téléchargement existant.', 'Mavjud yuklab olish papkasini tanlang.', 'Выберыце існуючую папку загрузак.'),
    'Нажав на Enter': ('Press Enter', 'Pulsar Enter', 'Pressionar Enter', 'Appuyer sur Entrée', 'Enter tugmasini bosish', 'Націснуць Enter'),
    'Нажав на Ctrl + Enter': ('Press Ctrl + Enter', 'Pulsar Ctrl + Enter', 'Pressionar Ctrl + Enter', 'Appuyer sur Ctrl + Entrée', 'Ctrl + Enter tugmalarini bosish', 'Націснуць Ctrl + Enter'),
    'ОТПРАВЛЯТЬ СООБЩЕНИЕ В ЧАТЕ': ('SEND CHAT MESSAGES', 'ENVIAR MENSAJES EN EL CHAT', 'ENVIAR MENSAGENS NO CHAT', 'ENVOYER LES MESSAGES DU CHAT', 'CHATDA XABAR YUBORISH', 'АДПРАЎЛЯЦЬ ПАВЕДАМЛЕННІ Ў ЧАЦЕ'),
    'ПОИСК И НАВИГАЦИЯ': ('SEARCH AND NAVIGATION', 'BÚSQUEDA Y NAVEGACIÓN', 'PESQUISA E NAVEGAÇÃO', 'RECHERCHE ET NAVIGATION', 'QIDIRUV VA NAVIGATSIYA', 'ПОШУК І НАВІГАЦЫЯ'),
    'Быстрый поиск по чатам': ('Quick chat search', 'Búsqueda rápida de chats', 'Pesquisa rápida nos chats', 'Recherche rapide des chats', 'Chatlarda tezkor qidiruv', 'Хуткі пошук па чатах'),
    'Поиск в загруженных сообщениях чата': ('Search loaded chat messages', 'Buscar en mensajes cargados del chat', 'Pesquisar nas mensagens carregadas do chat', 'Rechercher dans les messages chargés du chat', 'Yuklangan chat xabarlarida qidirish', 'Пошук у загружаных паведамленнях чата'),
    'Показаны доступные сочетания.': ('Available shortcuts are shown.', 'Se muestran los atajos disponibles.', 'Os atalhos disponíveis são exibidos.', 'Les raccourcis disponibles sont affichés.', 'Mavjud yorliqlar ko‘rsatilgan.', 'Паказаны даступныя спалучэнні.'),
    'Shift + Enter — новая строка.': ('Shift + Enter — new line.', 'Shift + Enter — nueva línea.', 'Shift + Enter — nova linha.', 'Maj + Entrée — nouvelle ligne.', 'Shift + Enter — yangi qator.', 'Shift + Enter — новы радок.'),
    'Открыть сайт MAX': ('Open the MAX website', 'Abrir el sitio de MAX', 'Abrir o site do MAX', 'Ouvrir le site MAX', 'MAX saytini ochish', 'Адкрыць сайт MAX'),
    'Неофициальный клиент MAX.': ('Unofficial MAX client.', 'Cliente no oficial de MAX.', 'Cliente não oficial do MAX.', 'Client MAX non officiel.', 'Norasmiy MAX mijozi.', 'Неафіцыйны кліент MAX.'),
    'Войти в Сферум': ('Sign in to Sferum', 'Acceder a Sferum', 'Entrar no Sferum', 'Se connecter à Sferum', 'Sferumga kirish', 'Увайсці ў Сферум'),
    'Сферум': ('Sferum', 'Sferum', 'Sferum', 'Sferum', 'Sferum', 'Сферум'),
    'Скопируйте ссылку на MAX и отправьте её удобным способом.': ('Copy the MAX link and share it however you prefer.', 'Copia el enlace de MAX y compártelo como prefieras.', 'Copie o link do MAX e compartilhe como preferir.', 'Copiez le lien MAX et partagez-le comme vous le souhaitez.', 'MAX havolasini nusxalab, qulay usulda ulashing.', 'Скапіюйце спасылку на MAX і адпраўце яе зручным спосабам.'),
    'Вход в образовательный профиль Сферум пока доступен в официальном MAX. Этот клиент не переключает образовательные аккаунты.': ('Sign in to a Sferum education profile in the official MAX app. This client does not switch education accounts.', 'Accede al perfil educativo de Sferum en la aplicación oficial de MAX. Este cliente no cambia cuentas educativas.', 'Entre no perfil educacional do Sferum no aplicativo oficial do MAX. Este cliente não alterna contas educacionais.', 'Connectez-vous au profil éducatif Sferum dans l’application officielle MAX. Ce client ne change pas de compte éducatif.', 'Sferum ta’lim profiliga rasmiy MAX ilovasida kiring. Bu mijoz ta’lim hisoblarini almashtirmaydi.', 'Увайдзіце ў адукацыйны профіль Сферум у афіцыйным MAX. Гэты кліент не пераключае адукацыйныя ўліковыя запісы.'),
    'Работа с бизнес-профилем пока не подключена в этом клиенте. Используйте официальный MAX для настройки бизнес-профиля.': ('Business profiles are not available in this client yet. Use the official MAX app to set up a business profile.', 'Los perfiles de empresa aún no están disponibles en este cliente. Usa MAX oficial para configurarlos.', 'Os perfis comerciais ainda não estão disponíveis neste cliente. Use o MAX oficial para configurá-los.', 'Les profils professionnels ne sont pas encore disponibles dans ce client. Configurez-les dans l’application officielle MAX.', 'Bu mijozda biznes profillari hozircha mavjud emas. Biznes profilini sozlash uchun rasmiy MAX ilovasidan foydalaning.', 'Бізнес-профілі пакуль не падключаны ў гэтым кліенце. Наладжвайце бізнес-профіль у афіцыйным MAX.'),
    'При выборе темы изменяются основные цвета, чаты и системные элементы. Изменения темы не синхронизируются между устройствами.': ('Choosing a theme changes the main colors, chats and system elements. Theme changes are not synced between devices.', 'Al elegir un tema cambian los colores principales, los chats y los elementos del sistema. Los temas no se sincronizan entre dispositivos.', 'Escolher um tema altera as cores principais, os chats e os elementos do sistema. Os temas não são sincronizados entre dispositivos.', 'Le thème modifie les couleurs principales, les chats et les éléments du système. Les thèmes ne sont pas synchronisés entre les appareils.', 'Mavzu tanlanganda asosiy ranglar, chatlar va tizim elementlari o‘zgaradi. Mavzu qurilmalar o‘rtasida sinxronlanmaydi.', 'Выбар тэмы змяняе асноўныя колеры, чаты і сістэмныя элементы. Тэмы не сінхранізуюцца паміж прыладамі.'),
    'В локальной истории: {count} чатов.\nЗагруженные файлы сохраняются в выбранную папку.': ('Chats in local history: {count}.\nDownloaded files are saved to the selected folder.', 'Chats en el historial local: {count}.\nLos archivos descargados se guardan en la carpeta seleccionada.', 'Chats no histórico local: {count}.\nOs arquivos baixados são salvos na pasta selecionada.', 'Chats dans l’historique local : {count}.\nLes fichiers téléchargés sont enregistrés dans le dossier choisi.', 'Mahalliy tarixdagi chatlar: {count}.\nYuklangan fayllar tanlangan papkaga saqlanadi.', 'Чатаў у лакальнай гісторыі: {count}.\nСпампаваныя файлы захоўваюцца ў выбранай папцы.'),
    'Сессия изменилась. Обновите настройки.': ('The session changed. Refresh settings.', 'La sesión ha cambiado. Actualiza los ajustes.', 'A sessão mudou. Atualize as configurações.', 'La session a changé. Actualisez les paramètres.', 'Seans o‘zgardi. Sozlamalarni yangilang.', 'Сеанс змяніўся. Абнавіце налады.'),
    'Не удалось загрузить настройки. Попробуйте обновить позже.': ('Could not load settings. Try refreshing later.', 'No se pudieron cargar los ajustes. Actualízalos más tarde.', 'Não foi possível carregar as configurações. Tente atualizar mais tarde.', 'Impossible de charger les paramètres. Réessayez plus tard.', 'Sozlamalarni yuklab bo‘lmadi. Keyinroq yangilab ko‘ring.', 'Не ўдалося загрузіць налады. Паспрабуйце абнавіць пазней.'),
    'Запрос принят. Изменение ещё не подтверждено MAX.': ('Request accepted. MAX has not confirmed the change yet.', 'Solicitud aceptada. MAX aún no ha confirmado el cambio.', 'Solicitação aceita. O MAX ainda não confirmou a alteração.', 'Demande acceptée. MAX n’a pas encore confirmé la modification.', 'So‘rov qabul qilindi. MAX o‘zgarishni hali tasdiqlamadi.', 'Запыт прыняты. MAX яшчэ не пацвердзіў змену.'),
    'Изменение ещё не подтверждено MAX. Показаны известные значения.': ('MAX has not confirmed the change yet. Known values are shown.', 'MAX aún no ha confirmado el cambio. Se muestran los valores conocidos.', 'O MAX ainda não confirmou a alteração. Os valores conhecidos são exibidos.', 'MAX n’a pas encore confirmé la modification. Les valeurs connues sont affichées.', 'MAX o‘zgarishni hali tasdiqlamadi. Ma’lum qiymatlar ko‘rsatilgan.', 'MAX яшчэ не пацвердзіў змену. Паказаны вядомыя значэнні.'),
    'Не удалось подтвердить изменение. Проверьте состояние перед повтором.': ('Could not confirm the change. Check the status before retrying.', 'No se pudo confirmar el cambio. Comprueba el estado antes de reintentarlo.', 'Não foi possível confirmar a alteração. Verifique o estado antes de tentar novamente.', 'Impossible de confirmer la modification. Vérifiez l’état avant de réessayer.', 'O‘zgarishni tasdiqlab bo‘lmadi. Qayta urinishdan oldin holatni tekshiring.', 'Не ўдалося пацвердзіць змену. Праверце стан перад паўторам.'),
    'Открытие семейной защиты пока недоступно в этом клиенте.': ('Family protection management is not available in this client yet.', 'La gestión de protección familiar aún no está disponible en este cliente.', 'A gestão da proteção familiar ainda não está disponível neste cliente.', 'La gestion de la protection familiale n’est pas encore disponible dans ce client.', 'Bu mijozda oilaviy himoyani boshqarish hozircha mavjud emas.', 'Кіраванне сямейнай абаронай пакуль недаступна ў гэтым кліенце.'),
    'Эта настройка управляется безопасным режимом или семейной защитой.': ('This setting is controlled by safe mode or family protection.', 'Este ajuste lo controla el modo seguro o la protección familiar.', 'Esta configuração é controlada pelo modo seguro ou pela proteção familiar.', 'Ce paramètre est géré par le mode sécurisé ou la protection familiale.', 'Bu sozlama xavfsiz rejim yoki oilaviy himoya orqali boshqariladi.', 'Гэтай наладай кіруе бяспечны рэжым або сямейная абарона.'),
}

_ALIASES = {
    'Выйти на всех устройствах, кроме текущего': 'Выйти на всех устройствах, кроме этого',
    'Завершить другие сеансы?': 'Выйти на всех устройствах, кроме этого?',
    'Сохранить изменения': 'Сохранить изменения',
}


def normalize_language(value) -> str:
    if not isinstance(value, str):
        return 'ru'
    normalized = value.strip().lower().replace('_', '-')
    if normalized in ('pt-br', 'pt'):
        return 'pt'
    if normalized in SUPPORTED_LANGUAGES:
        return normalized
    return 'ru'


def _source_key(source: str) -> str:
    return ' '.join(source.replace('\xa0', ' ').split())


@lru_cache(maxsize=6)
def _catalog(language: str) -> dict[str, str]:
    if language == 'ru':
        return {}
    data = {_source_key(source): value for source, value in OFFICIAL[language].items()}
    index = SUPPORTED_LANGUAGES[1:].index(language)
    data.update({_source_key(source): values[index] for source, values in _OWN.items()})
    data.update({_source_key(source): values[index] for source, values in FOLDER_EDITOR_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONTACT_DIALOG_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in SETTINGS_DIALOG_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in MESSAGE_ACTION_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONTACTS_WORKSPACE_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONVERSATION_MANAGEMENT_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONVERSATION_INFO_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CHAT_ROW_REASON_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONTACT_EDITOR_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CONVERSATION_CREATE_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in FILE_DOWNLOAD_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in PHOTO_VIEWER_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in SEARCH_TRANSLATIONS.items()})
    data.update({_source_key(source): values[index] for source, values in CODES_DIALOG_TRANSLATIONS.items()})
    for source, original in _ALIASES.items():
        if _source_key(original) in data:
            data[_source_key(source)] = data[_source_key(original)]
    return data


def _translated(source: str, language: str):
    data = _catalog(language)
    key = _source_key(source)
    if key in data:
        return data[key]
    # Native labels sometimes differ only in case from the source catalogue.
    title = key[:1].upper() + key[1:].lower()
    if (key.isupper() or key.islower()) and title in data:
        text = data[title]
        return text.upper() if key.isupper() else text[:1].lower() + text[1:]
    return None


def translation_available(source: str, language: str = 'ru') -> bool:
    language = normalize_language(language)
    return language == 'ru' or _translated(source, language) is not None


def tr(source: str, language: str = 'ru', **values) -> str:
    """Translate one explicitly supplied app-owned string; unknowns retain source.

    Parameters are inserted only after choosing a template, so names, messages,
    phone numbers and other values never become catalogue lookup keys.
    """
    if not isinstance(source, str):
        raise TypeError('UI translation source must be text')
    language = normalize_language(language)
    translated = _translated(source, language) if language != 'ru' else None
    if translated is None:
        translated = source
    elif source != source.strip():
        translated = source[:len(source) - len(source.lstrip())] + translated.strip() + source[len(source.rstrip()):]
    return translated.format(**values) if values else translated


def language_rows(language='ru'):
    """Native language list, including its observed Russian French title."""
    language = normalize_language(language)
    return tuple((code, title, tr(subtitle, language)) for code, title, subtitle in
                 zip(SUPPORTED_LANGUAGES, LANGUAGE_TITLES, LANGUAGE_SUBTITLES))
