/**
 * aitrack — Google Sheets vastuvõtja (Apps Script veebirakendus).
 *
 * PAIGALDUS (üks kord, ~5 min):
 *  1. Loo uus Google Sheet (sheets.new).
 *  2. Menüü: Laiendused → Apps Script.
 *  3. Kustuta näidiskood ja kleebi siia kogu see fail.
 *  4. Muuda allpool SECRET väärtus millekski juhuslikuks (sama pane config.json token-väljale).
 *  5. Klõpsa "Juuruta" (Deploy) → "Uus juurutus" → tüüp "Veebirakendus".
 *       - Käivita kasutajana: Mina
 *       - Kellel on juurdepääs: Kõik (Anyone)
 *  6. Kopeeri "Veebirakenduse URL" → pane see config.json webapp_url väljale.
 *
 * TURVALISUS: URL + token on koos kirjutusõigus sellele lehele. ÄRA committi neid
 * koodi ega jaga avalikult. Kuna juurdepääs on "Kõik", on token AINUS kaitse —
 * vali pikk juhuslik väärtus ja hoia config.json privaatsena (aitrack teeb selle 0o600).
 *
 * Edaspidi lisab aitrack ridu automaatselt. Töötab append-only, ei kustuta midagi.
 */

var SECRET = "MUUDA-SEE-ARA";       // <-- pane sama väärtus config.json token-väljale
var SHEET_NAME = "Log";
var HEADERS = ["Kuupäev", "Tund", "Projekt", "Tööriist", "Töö kokkuvõte"];
// Nähtamatu dedup-võtme veerg (HEADERS järel). Võid selle Sheetsis ära peita.

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var data = JSON.parse(e.postData.contents);
    if (SECRET === "MUUDA-SEE-ARA") {        // ebaturvaline vaikesaladus — keeldu valjult
      return ContentService.createTextOutput("error:default-secret");
    }
    if (data.token !== SECRET) {
      return ContentService.createTextOutput("forbidden");
    }
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var keyCol = HEADERS.length + 1;           // _key veeru indeks

    // op:"keys" → tagasta olemasolevad rea-võtmed (backfill ei summeeri neid uuesti)
    if (data.op === "keys") {
      var sh = ss.getSheetByName(SHEET_NAME);
      var out = [];
      if (sh && sh.getLastRow() >= 2) {
        var kv = sh.getRange(2, keyCol, sh.getLastRow() - 1, 1).getValues();
        for (var i = 0; i < kv.length; i++) { out.push(String(kv[i][0])); }
      }
      return ContentService.createTextOutput(JSON.stringify(out))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(HEADERS.concat(["_key"]));
      sheet.getRange(1, 1, 1, HEADERS.length).setFontWeight("bold");
      sheet.setFrozenRows(1);
      // sunni _key veerg tekstiks, et Sheets ei coerciks võtit kuupäevaks/numbriks
      sheet.getRange(1, keyCol, sheet.getMaxRows(), 1).setNumberFormat("@");
    }
    // Loe olemasolevad võtmed → idempotentsus (katkestus/kordussaatmine ei tekita duplikaate)
    var existing = {};
    var last = sheet.getLastRow();
    if (last >= 2) {
      var vals = sheet.getRange(2, keyCol, last - 1, 1).getValues();
      for (var i = 0; i < vals.length; i++) { existing[String(vals[i][0])] = true; }
    }
    var rows = data.rows || (data.row ? [data.row] : []);
    var keys = data.keys || [];
    if (!Array.isArray(rows) || !Array.isArray(keys)) {
      return ContentService.createTextOutput("error:bad-payload");  // väldi vale-edu (ok:0)
    }
    // Nõua võtit igale reale — muidu kaoks idempotentsus (kordussaatmine dubleeriks)
    for (var n = 0; n < rows.length; n++) {
      if (!keys[n]) { return ContentService.createTextOutput("error:missing-key"); }
    }
    var added = 0;
    for (var j = 0; j < rows.length; j++) {
      var k = keys[j];
      if (existing[k]) { continue; }           // juba olemas — jäta vahele
      sheet.appendRow(rows[j].concat([k]));
      existing[k] = true;
      added++;
    }
    return ContentService.createTextOutput("ok:" + added);
  } catch (err) {
    return ContentService.createTextOutput("error:" + err);
  } finally {
    lock.releaseLock();
  }
}

// Lubab brauseris URL-i avades kiirelt kontrollida, kas rakendus elab.
function doGet() {
  return ContentService.createTextOutput("aitrack sink alive");
}
