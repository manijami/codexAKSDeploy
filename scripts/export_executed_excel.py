"""Generic Excel writer; private campaign exporters are not published."""
import zipfile,re,math,json
from xml.sax.saxutils import escape
def compact(v):return json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v

def write_xlsx(path,sheets):
    """OOXML inline strings: no macros, formulas, external links or dependencies."""
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    def col(n):
        out=''
        while n:n,r=divmod(n-1,26);out=chr(65+r)+out
        return out
    def xmltext(v):return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(v)))
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))+'</Types>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'+''.join(f'<sheet name="{xmltext(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="styles" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF164B64"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs></styleSheet>')
        for i,(name,rows) in enumerate(sheets,1):
            count=len(rows[0]);end=f'{col(count)}{len(rows)}';parts=[]
            for ri,row in enumerate(rows,1):
                cells=[]
                for ci,val in enumerate(row,1):
                    ref=f'{col(ci)}{ri}';style='1' if ri==1 else '0'
                    if val is None:continue
                    if isinstance(val,(int,float)) and not isinstance(val,bool) and math.isfinite(val):cells.append(f'<c r="{ref}" s="{style}"><v>{val}</v></c>')
                    else:
                        text=str(compact(val));assert len(text)<=32767,f'Excel cell limit: {name} {ref}'
                        cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{xmltext(text)}</t></is></c>')
                parts.append(f'<row r="{ri}">'+''.join(cells)+'</row>')
            widths=''.join(f'<col min="{j}" max="{j}" width="{55 if any(x in str(rows[0][j-1]).lower() for x in ("answer","question","reason","passage","expected","notes")) else 24}" customWidth="1"/>' for j in range(1,count+1))
            z.writestr(f'xl/worksheets/sheet{i}.xml',f'<worksheet xmlns="{ns}"><dimension ref="A1:{end}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{widths}</cols><sheetData>'+''.join(parts)+f'</sheetData><autoFilter ref="A1:{end}"/></worksheet>')
