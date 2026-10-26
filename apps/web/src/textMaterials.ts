export async function readTextFile(file: Pick<File, "name" | "size" | "arrayBuffer">) {
  if (!file.name.toLowerCase().endsWith(".txt"))
    throw new Error("Выберите файл с расширением .txt.");
  if (file.size > 2_000_000)
    throw new Error("Файл должен быть не больше 2 МБ.");
  let content: string;
  try {
    content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch {
    throw new Error("Не удалось прочитать файл. Сохраните его в UTF-8.");
  }
  if (!content.trim()) throw new Error("Файл пуст. Добавьте текст для чтения.");
  if (Array.from(content).length > 200_000)
    throw new Error("Текст должен содержать не больше 200 000 символов.");
  return { content, title: file.name.replace(/\.txt$/i, "") };
}
