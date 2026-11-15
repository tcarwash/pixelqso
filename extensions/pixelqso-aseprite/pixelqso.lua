-- Pixel QSO card tools for Aseprite. RGB444 shared palettes and animation links.
local M = {}
local function palette(n)
  if n == 2 then return {{0,0,0},{15,15,15}} end
  if n == 8 then return {{0,0,0},{15,15,15},{0,0,15},{0,15,15},{0,15,0},{15,15,0},{15,0,0},{15,0,15}} end
  local out = {}
  local gs = n == 16 and {0,15} or {0,5,10,15}
  for _,r in ipairs{0,5,10,15} do for _,g in ipairs(gs) do for _,b in ipairs{0,15} do
    out[#out+1] = {r,g,b}
  end end end
  return out
end
M.palette = palette

-- BLAKE2s, parameterized for the eight-byte digest used by card_animation.py.
local iv = {0x6A09E667,0xBB67AE85,0x3C6EF372,0xA54FF53A,0x510E527F,0x9B05688C,0x1F83D9AB,0x5BE0CD19}
local sigma = {{0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15},{14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3},{11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4},{7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8},{9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13},{2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9},{12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11},{13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10},{6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5},{10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0}}
local function ror(x,n) return ((x >> n) | (x << (32-n))) & 0xffffffff end
local function blake8(s)
  local h = {table.unpack(iv)}; h[1] = h[1] ~ 0x01010008
  local count = 0
  repeat
    local chunk = s:sub(count+1,count+64); count = count + #chunk
    local last = count == #s; chunk = chunk .. string.rep('\0',64-#chunk)
    local m = {}; for i=1,64,4 do m[#m+1] = string.unpack('<I4',chunk,i) end
    local v = {}; for i=1,8 do v[i]=h[i]; v[i+8]=iv[i] end
    v[13]=v[13] ~ count; if last then v[15]=v[15] ~ 0xffffffff end
    local function g(a,b,c,d,x,y)
      v[a]=(v[a]+v[b]+x)&0xffffffff; v[d]=ror(v[d] ~ v[a],16)
      v[c]=(v[c]+v[d])&0xffffffff; v[b]=ror(v[b] ~ v[c],12)
      v[a]=(v[a]+v[b]+y)&0xffffffff; v[d]=ror(v[d] ~ v[a],8)
      v[c]=(v[c]+v[d])&0xffffffff; v[b]=ror(v[b] ~ v[c],7)
    end
    for _,q in ipairs(sigma) do
      g(1,5,9,13,m[q[1]+1],m[q[2]+1]); g(2,6,10,14,m[q[3]+1],m[q[4]+1])
      g(3,7,11,15,m[q[5]+1],m[q[6]+1]); g(4,8,12,16,m[q[7]+1],m[q[8]+1])
      g(1,6,11,16,m[q[9]+1],m[q[10]+1]); g(2,7,12,13,m[q[11]+1],m[q[12]+1])
      g(3,8,9,14,m[q[13]+1],m[q[14]+1]); g(4,5,10,15,m[q[15]+1],m[q[16]+1])
    end
    for i=1,8 do h[i]=(h[i] ~ v[i] ~ v[i+8]) & 0xffffffff end
  until count == #s
  return (string.pack('<I4I4',h[1],h[2]):gsub('.',function(c) return string.format('%02x',string.byte(c)) end))
end
M.blake8 = blake8
local function quote(s) return '"' .. s:gsub('[%z\1-\31\\"]', function(c) return string.format('\\u%04x',c:byte()) end) .. '"' end
local function array(t) return '['..table.concat(t,',')..']' end
local function card_json(card)
  local p={}; for _,c in ipairs(card.palette) do p[#p+1]=array(c) end
  local anim='null'
  if card.animation then local a=card.animation; anim=string.format('{"first_card_id":%d,"first_digest":%s,"frame_ms":%d}',a.first_card_id,quote(a.first_digest),a.frame_ms) end
  return string.format('{"callsign":%s,"grid":%s,"card_id":%d,"width":%d,"height":%d,"palette":%s,"pixels":%s,"animation":%s}\n',quote(card.callsign),quote(card.grid),card.card_id,card.width,card.height,array(p),array(card.pixels),anim)
end
function M.digest(card)
  local identity=string.format('[%s,%d,%d,%d,%d]',quote(card.callsign),card.card_id,card.width,card.height,#card.palette)
  local p={}; for _,v in ipairs(card.pixels) do p[#p+1]=string.char(v) end
  return blake8(identity..table.concat(p))
end
function M.create(size,n,frames)
  local s=Sprite(size,size,ColorMode.RGB)
  local p=Palette(n); for i,c in ipairs(palette(n)) do p:setColor(i-1,Color{r=c[1]*17,g=c[2]*17,b=c[3]*17,a=255}) end
  s:setPalette(p)
  local image=Image(size,size,ColorMode.RGB); image:clear(app.pixelColor.rgba(0,0,0,255))
  local cel=s.cels[1]; cel.image:drawImage(image,Point(0,0))
  if frames == 2 then s:newFrame(1) end
  app.activeSprite=s
  return s
end
function M.build(sprite,n,call,grid,id1,id2,ms,frames)
  assert(sprite.width==sprite.height and (sprite.width==16 or sprite.width==32 or sprite.width==64), 'Canvas must be 16×16, 32×32, or 64×64.')
  assert(n==2 or n==8 or n==16 or n==32,'Choose a supported palette.')
  call=call:upper(); grid=grid:upper()
  assert(#call<=12 and #grid<=8 and not call:find('[^ -~]') and not grid:find('[^ -~]'), 'Call sign and grid must be ASCII (at most 12 and 8 characters).')
  assert(id1 and id1%1==0 and id1>=0 and id1<=65535,'First card ID must be 0–65535.')
  assert(frames==1 or frames==2,'Choose one or two frames.')
  assert(#sprite.frames==frames, 'The sprite must contain exactly '..frames..' frame(s).')
  if frames==2 then
    assert(id2 and id2%1==0 and id2>=0 and id2<=65535 and id1~=id2,'Animation card IDs must be different numbers from 0–65535.')
    assert(ms and ms%1==0 and ms>=100 and ms<=10000,'Frame duration must be 100–10000 ms.')
  end
  local pal=palette(n); local lookup={}
  for i,c in ipairs(pal) do lookup[string.format('%d,%d,%d',c[1]*17,c[2]*17,c[3]*17)]=i-1 end
  local cards={}
  for frame=1,frames do
    local img=Image(sprite.width,sprite.height,ColorMode.RGB); img:drawSprite(sprite,frame)
    local pixels={}
    for y=0,sprite.height-1 do for x=0,sprite.width-1 do
      local px=img:getPixel(x,y); local a=app.pixelColor.rgbaA(px)
      assert(a==255, string.format('Frame %d pixel (%d,%d) is transparent. Cards must be opaque.',frame,x+1,y+1))
      local key=string.format('%d,%d,%d',app.pixelColor.rgbaR(px),app.pixelColor.rgbaG(px),app.pixelColor.rgbaB(px))
      local index=lookup[key]; assert(index~=nil,string.format('Frame %d pixel (%d,%d) is outside the selected palette.',frame,x+1,y+1))
      pixels[#pixels+1]=index
    end end
    cards[frame]={callsign=call,grid=grid,card_id=frame==1 and id1 or id2,width=sprite.width,height=sprite.height,palette=pal,pixels=pixels}
  end
  if frames==2 then cards[2].animation={first_card_id=id1,first_digest=M.digest(cards[1]),frame_ms=ms} end
  return cards
end
function M.write(cards,path)
  if not path:lower():match('%.json$') then path=path..'.json' end
  local paths={path}; if #cards==2 then paths[2]=path:gsub('%.[jJ][sS][oO][nN]$','')..'-frame2.json' end
  for _,p in ipairs(paths) do local f=io.open(p,'rb'); if f then f:close(); error('File already exists: '..p..'\nChoose a new filename to avoid overwriting cards.') end end
  for i,p in ipairs(paths) do local f,e=io.open(p,'wb'); if not f then error(e) end; local ok,err=f:write(card_json(cards[i])); f:close(); if not ok then error(err) end end
  return paths
end
local function new_dialog()
  local d=Dialog{title='New Pixel QSO card'}
  d:combobox{id='size',label='Canvas',options={'16','32','64'},option='32'}
   :combobox{id='colors',label='Colors',options={'2','8','16','32'},option='8'}
   :combobox{id='frames',label='Frames',options={'1','2'},option='1'}
   :button{id='ok',text='Create'}:button{text='Cancel'}:show()
  if d.data.ok then M.create(tonumber(d.data.size),tonumber(d.data.colors),tonumber(d.data.frames)) end
end
local function export_dialog()
  local s=app.activeSprite; if not s then app.alert('Open a sprite first.'); return end
  local d=Dialog{title='Export Pixel QSO card'}
  d:entry{id='call',label='Call sign',text=''}:entry{id='grid',label='Grid',text=''}
   :combobox{id='colors',label='Palette',options={'2','8','16','32'},option=tostring((#s.palettes[1]==2 or #s.palettes[1]==8 or #s.palettes[1]==16 or #s.palettes[1]==32) and #s.palettes[1] or 8)}
   :combobox{id='frames',label='Frames',options={'1','2'},option=#s.frames==2 and '2' or '1'}
   :number{id='id1',label='First card ID',text=tostring(math.random(0,65535)),decimals=0}
   :number{id='id2',label='Second card ID',text=tostring(math.random(0,65535)),decimals=0}
   :number{id='ms',label='Frame time (ms)',text='500',decimals=0}
   :file{id='path',label='Card JSON',save=true,filetypes={'json'}}
   :button{id='ok',text='Export'}:button{text='Cancel'}:show()
  if not d.data.ok then return end
  local ok,result=pcall(function()
    assert(d.data.path and d.data.path~='', 'Choose an output filename.')
    return M.write(M.build(s,tonumber(d.data.colors),d.data.call,d.data.grid,d.data.id1,d.data.id2,d.data.ms,tonumber(d.data.frames)),d.data.path)
  end)
  if ok then app.alert('Exported:\n'..table.concat(result,'\n')) else app.alert('Could not export:\n'..tostring(result)) end
end
function init(plugin)
  plugin:newMenuGroup{id='pixelqso_tools',title='Pixel QSO',group='file_scripts'}
  plugin:newCommand{id='PixelQSONew',title='New Card…',group='pixelqso_tools',onclick=new_dialog}
  plugin:newCommand{id='PixelQSOExport',title='Export Card…',group='pixelqso_tools',onclick=export_dialog}
end
function exit(plugin) end
return M
