"""App-owned file download wording, not official MAX translations.

Columns: en, es, pt-BR, fr, uz, be. File names and destinations remain data.
"""

FILE_DOWNLOAD_TRANSLATIONS = {
    'Файл удалён': (
        'File deleted', 'Archivo eliminado', 'Arquivo excluído',
        'Fichier supprimé', 'Fayl o‘chirilgan', 'Файл выдалены'),
    'Скачивание недоступно': (
        'Download unavailable', 'Descarga no disponible', 'Download indisponível',
        'Téléchargement indisponible', 'Yuklab olish mavjud emas', 'Спампоўванне недаступнае'),
    'Действия с файлом': (
        'File actions', 'Acciones del archivo', 'Ações do arquivo',
        'Actions sur le fichier', 'Fayl amallari', 'Дзеянні з файлам'),
    'Копировать имя файла': (
        'Copy file name', 'Copiar nombre del archivo', 'Copiar nome do arquivo',
        'Copier le nom du fichier', 'Fayl nomini nusxalash', 'Скапіяваць назву файла'),
    'Скачивание…': (
        'Downloading…', 'Descargando…', 'Baixando…', 'Téléchargement…', 'Yuklab olinmoqda…', 'Спампоўванне…'),
    'Сохранение файла…': (
        'Saving file…', 'Guardando archivo…', 'Salvando arquivo…',
        'Enregistrement du fichier…', 'Fayl saqlanmoqda…', 'Захаванне файла…'),
    'Файл сохранён': (
        'File saved', 'Archivo guardado', 'Arquivo salvo',
        'Fichier enregistré', 'Fayl saqlandi', 'Файл захаваны'),
    'Не удалось скачать файл': (
        'Could not download the file', 'No se pudo descargar el archivo',
        'Não foi possível baixar o arquivo', 'Impossible de télécharger le fichier',
        'Faylni yuklab olib bo‘lmadi', 'Не ўдалося спампаваць файл'),
    'Скачивание отменено': (
        'Download cancelled', 'Descarga cancelada', 'Download cancelado',
        'Téléchargement annulé', 'Yuklab olish bekor qilindi', 'Спампоўванне скасавана'),
    'Отмена…': (
        'Cancelling…', 'Cancelando…', 'Cancelando…', 'Annulation…', 'Bekor qilinmoqda…', 'Скасаванне…'),
    'Б': ('B', 'B', 'B', 'o', 'B', 'Б'),
    'КБ': ('KB', 'KB', 'KB', 'Ko', 'KB', 'КБ'),
    'МБ': ('MB', 'MB', 'MB', 'Mo', 'MB', 'МБ'),
    'ГБ': ('GB', 'GB', 'GB', 'Go', 'GB', 'ГБ'),
    'ТБ': ('TB', 'TB', 'TB', 'To', 'TB', 'ТБ'),
    'Размер не передан': (
        'Size not provided', 'Tamaño no proporcionado', 'Tamanho não informado',
        'Taille non fournie', 'Hajmi berilmagan', 'Памер не перададзены'),
    'Прогресс скачивания файла': (
        'File download progress', 'Progreso de la descarga del archivo', 'Progresso do download do arquivo',
        'Progression du téléchargement du fichier', 'Faylni yuklab olish jarayoni', 'Ход спампоўвання файла'),
    'К сообщению': (
        'Go to message', 'Ir al mensaje', 'Ir para a mensagem',
        'Aller au message', 'Xabarga o‘tish', 'Да паведамлення'),
    '{received} из {total}': (
        '{received} of {total}', '{received} de {total}', '{received} de {total}',
        '{received} sur {total}', '{total} dan {received}', '{received} з {total}'),
    'Файл может быть небезопасен': (
        'This file may be unsafe', 'Este archivo puede ser peligroso', 'Este arquivo pode ser perigoso',
        'Ce fichier peut être dangereux', 'Bu fayl xavfli bo‘lishi mumkin', 'Файл можа быць небяспечным'),
    'MAX предупредил о возможной опасности файла. Скачивайте его, только если доверяете отправителю.': (
        'MAX warned that this file may be unsafe. Download it only if you trust the sender.',
        'MAX advirtió que este archivo puede ser peligroso. Descárgalo solo si confías en quien lo envió.',
        'O MAX avisou que este arquivo pode ser perigoso. Baixe-o somente se confiar no remetente.',
        'MAX a signalé que ce fichier peut être dangereux. Téléchargez-le uniquement si vous faites confiance à l’expéditeur.',
        'MAX fayl xavfli bo‘lishi mumkinligi haqida ogohlantirdi. Uni faqat yuboruvchiga ishonsangiz yuklab oling.',
        'MAX папярэдзіў пра магчымую небяспеку файла. Спампоўвайце яго, толькі калі давяраеце адпраўніку.'),
    'Все файлы (*)': (
        'All files (*)', 'Todos los archivos (*)', 'Todos os arquivos (*)',
        'Tous les fichiers (*)', 'Barcha fayllar (*)', 'Усе файлы (*)'),
    'Не удалось выбрать место для сохранения файла.': (
        'Could not choose where to save the file.', 'No se pudo elegir dónde guardar el archivo.',
        'Não foi possível escolher onde salvar o arquivo.', 'Impossible de choisir où enregistrer le fichier.',
        'Faylni saqlash joyini tanlab bo‘lmadi.', 'Не ўдалося выбраць месца для захавання файла.'),
    'Файл больше недоступен. Обновите список.': (
        'The file is no longer available. Refresh the list.', 'El archivo ya no está disponible. Actualiza la lista.',
        'O arquivo não está mais disponível. Atualize a lista.', 'Le fichier n’est plus disponible. Actualisez la liste.',
        'Fayl endi mavjud emas. Ro‘yxatni yangilang.', 'Файл больш недаступны. Абнавіце спіс.'),
    'Дождитесь завершения предыдущей загрузки.': (
        'Wait for the previous download to finish.', 'Espera a que termine la descarga anterior.',
        'Aguarde o término do download anterior.', 'Attendez la fin du téléchargement précédent.',
        'Oldingi yuklab olish tugashini kuting.', 'Дачакайцеся завяршэння папярэдняга спампоўвання.'),
}

__all__ = ['FILE_DOWNLOAD_TRANSLATIONS']
